//! 采集编排状态机（PRD F5）：IDLE→READY→ARMED→SWING→SAVING→READY 循环。
//! 语义对齐 Python 版 app/detect/state_machine.py。
//!
//! 纯逻辑、无 UI/Tauri 依赖；事件驱动（feed_frame 逐帧喂入）；
//! listener 回调与可选 Voice 播报供采集层接入。

use serde::Serialize;

use crate::capture::{BufferItem, RingBuffer};
use crate::detect::{PresenceDetect, SwingDetect, SwingEvent};
use crate::voice::{
    error_prompt, prompt_saved, Voice, PROMPT_DISCARDED, PROMPT_LEFT, PROMPT_READY, PROMPT_SWING,
    PROMPT_SWING_DONE,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum SmState {
    /// 无人
    #[serde(rename = "IDLE")]
    Idle,
    /// 就位，语音 + 倒计时
    #[serde(rename = "READY")]
    Ready,
    /// 待挥棒，预录中
    #[serde(rename = "ARMED")]
    Armed,
    /// 挥棒中
    #[serde(rename = "SWING")]
    Swing,
    /// 落盘
    #[serde(rename = "SAVING")]
    Saving,
    /// 相机/存储异常，可恢复
    #[serde(rename = "ERROR")]
    Error,
}

impl SmState {
    pub fn as_str(&self) -> &'static str {
        match self {
            SmState::Idle => "IDLE",
            SmState::Ready => "READY",
            SmState::Armed => "ARMED",
            SmState::Swing => "SWING",
            SmState::Saving => "SAVING",
            SmState::Error => "ERROR",
        }
    }

    fn is_working(&self) -> bool {
        matches!(
            self,
            SmState::Ready | SmState::Armed | SmState::Swing | SmState::Saving
        )
    }
}

/// 一段挥棒素材：帧区间 + 序号。帧序列由状态机在保存时从环形缓冲提取。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Clip {
    pub seq: u64,
    pub start_idx: u64,
    pub end_idx: u64,
    pub trigger_idx: u64,
}

impl Clip {
    pub fn frame_count(&self) -> u64 {
        self.end_idx - self.start_idx + 1
    }
}

#[derive(Debug, Clone)]
pub struct Transition {
    pub prev: SmState,
    pub next: SmState,
    pub reason: String,
    pub clip: Option<Clip>,
}

type TransitionListener = Box<dyn FnMut(&Transition) + Send>;
/// 片段保存回调：参数为片段元数据 + 已从环形缓冲提取的帧序列。
/// 对齐 Python 版 clip_saver 语义：返回 Err 时状态机回滚片段并转 ERROR。
type ClipSaver = Box<dyn FnMut(&Clip, Vec<BufferItem>) -> Result<(), String> + Send>;

/// 挥棒采集状态机。
pub struct CaptureStateMachine {
    presence: Box<dyn PresenceDetect>,
    swing: Box<dyn SwingDetect>,
    pub buffer: RingBuffer,
    pub fps: f64,
    voice: Option<Box<dyn Voice>>,
    clip_saver: Option<ClipSaver>,
    countdown_frames: u64,
    pub state: SmState,
    pub seq: u64,
    pub clips: Vec<Clip>,
    pub last_error: Option<String>,
    listeners: Vec<TransitionListener>,
    ready_elapsed: u64,
    countdown_next: i64,
    trigger_idx: i64,
}

impl CaptureStateMachine {
    pub fn new(
        presence: Box<dyn PresenceDetect>,
        swing: Box<dyn SwingDetect>,
        buffer: RingBuffer,
        fps: f64,
        voice: Option<Box<dyn Voice>>,
        clip_saver: Option<ClipSaver>,
        countdown_seconds: f64,
    ) -> Self {
        Self {
            presence,
            swing,
            buffer,
            fps,
            voice,
            clip_saver,
            countdown_frames: ((countdown_seconds * fps) + 0.5).max(1.0) as u64,
            state: SmState::Idle,
            seq: 0,
            clips: Vec::new(),
            last_error: None,
            listeners: Vec::new(),
            ready_elapsed: 0,
            countdown_next: 0,
            trigger_idx: -1,
        }
    }

    // ---- 外部接口 ----

    /// 注册迁移回调：fn(Transition)。listener 异常不打断状态机（panic 由调用方隔离）。
    pub fn add_listener(&mut self, f: TransitionListener) {
        self.listeners.push(f);
    }

    /// 就位占比（遥测用）
    pub fn presence_ratio(&self) -> f64 {
        self.presence.last_ratio()
    }

    /// 运动占比（遥测用）
    pub fn motion_ratio(&self) -> f64 {
        self.swing.last_ratio()
    }

    pub fn feed_frame(&mut self, frame_idx: u64, ts_ns: u64, left: crate::capture::GrayFrame, right: crate::capture::GrayFrame) {
        self.buffer.push(frame_idx, ts_ns, left.clone(), right);
        if self.state == SmState::Error {
            return;
        }
        let present = self.presence.update(&left);

        // 任意工作态人离开 → IDLE（SWING 中离开丢弃进行中片段）
        if !present && self.state.is_working() {
            self.swing.reset();
            self.speak(PROMPT_LEFT, 1);
            self.transition(SmState::Idle, "presence_lost", None);
            return;
        }

        match self.state {
            SmState::Idle => {
                if present {
                    self.ready_elapsed = 0;
                    // +1 使首个整秒 tick（如 "3"）在倒计时第一帧即播报
                    self.countdown_next = (self.countdown_frames as f64 / self.fps).ceil() as i64 + 1;
                    self.speak(PROMPT_READY, 0);
                    self.transition(SmState::Ready, "presence_settled", None);
                }
            }
            SmState::Ready => {
                self.ready_elapsed += 1;
                let remaining_s = (self.countdown_frames as f64 - self.ready_elapsed as f64) / self.fps;
                if remaining_s > 0.0 {
                    let tick = remaining_s.ceil() as i64;
                    if tick < self.countdown_next {
                        self.countdown_next = tick;
                        self.speak(&tick.to_string(), 0);
                    }
                    return;
                }
                self.speak(PROMPT_SWING, 0);
                self.transition(SmState::Armed, "countdown_done", None);
            }
            SmState::Armed => {
                if self.swing.update(&left, frame_idx) == SwingEvent::Started {
                    self.trigger_idx = self.swing.trigger_idx();
                    self.transition(SmState::Swing, "swing_started", None);
                }
            }
            SmState::Swing => {
                if self.trigger_idx >= 0 && self.swing.active() {
                    self.swing.update(&left, frame_idx);
                }
                if !self.swing.active() && self.trigger_idx >= 0 {
                    self.finish_swing(frame_idx, "swing_ended");
                }
            }
            _ => {}
        }
    }

    /// 手动开始（兜底）：ARMED → SWING。
    pub fn manual_start(&mut self, frame_idx: Option<u64>) {
        if self.state != SmState::Armed {
            return;
        }
        let idx = frame_idx.unwrap_or_else(|| self.buffer.latest().map(|it| it.0).unwrap_or(0));
        self.trigger_idx = idx as i64;
        self.swing.set_manual_active(true, idx as i64); // 手动模式：由 manual_stop 结束
        self.transition(SmState::Swing, "manual_start", None);
    }

    /// 手动结束（兜底）：SWING → SAVING → READY。
    pub fn manual_stop(&mut self, frame_idx: Option<u64>) {
        if self.state != SmState::Swing {
            return;
        }
        let idx = frame_idx.unwrap_or_else(|| {
            self.buffer
                .latest()
                .map(|it| it.0)
                .unwrap_or(self.trigger_idx.max(0) as u64)
        });
        self.swing.set_manual_active(false, self.swing.trigger_idx());
        self.finish_swing(idx, "manual_stop");
    }

    /// 语音静音开关（UI「静音」即时生效，不等下次采集）。
    pub fn set_muted(&mut self, muted: bool) {
        if let Some(v) = self.voice.as_mut() {
            v.set_muted(muted);
        }
    }

    /// 手动开始/结束二合一（UI 单按钮）：ARMED→SWING 返回 "started"，
    /// SWING→SAVING→READY 返回 "stopped"，其余状态不动作返回 "ignored"。
    pub fn manual_toggle(&mut self) -> &'static str {
        match self.state {
            SmState::Armed => {
                self.manual_start(None);
                "started"
            }
            SmState::Swing => {
                self.manual_stop(None);
                "stopped"
            }
            _ => "ignored",
        }
    }

    /// 误检丢弃重拍：SWING/ARMED → READY，不产出片段。
    pub fn discard(&mut self) {
        if !matches!(self.state, SmState::Swing | SmState::Armed) {
            return;
        }
        self.swing.reset();
        self.trigger_idx = -1;
        self.speak(PROMPT_DISCARDED, 1);
        self.transition(SmState::Ready, "discarded", None);
    }

    /// 暂停：任意工作态 → IDLE。
    pub fn pause(&mut self) {
        if matches!(self.state, SmState::Idle | SmState::Error) {
            return;
        }
        self.swing.reset();
        self.transition(SmState::Idle, "paused", None);
    }

    /// 异常：任意态 → ERROR。语音按消息分类（相机断开 / 存储失败 / 通用）。
    pub fn error(&mut self, message: &str) {
        self.last_error = Some(message.to_string());
        self.speak(error_prompt(message), 2);
        self.transition(SmState::Error, &format!("error: {message}"), None);
    }

    /// 异常恢复：ERROR → IDLE。
    pub fn recover(&mut self) {
        if self.state == SmState::Error {
            self.swing.reset();
            self.presence.reset();
            self.transition(SmState::Idle, "recovered", None);
        }
    }

    // ---- 内部 ----

    fn finish_swing(&mut self, end_idx: u64, reason: &str) {
        self.seq += 1;
        let trigger = self.trigger_idx.max(0) as u64;
        let mut start_idx = trigger.saturating_sub(self.swing.pre_roll_frames());
        if let Some((oldest, _)) = self.buffer.span() {
            start_idx = start_idx.max(oldest);
        }
        let clip = Clip {
            seq: self.seq,
            start_idx,
            end_idx,
            trigger_idx: trigger,
        };
        self.clips.push(clip.clone());
        self.trigger_idx = -1;
        self.swing.reset();
        self.speak(PROMPT_SWING_DONE, 0);
        self.transition(SmState::Saving, reason, Some(clip.clone()));
        if self.clip_saver.is_some() {
            let frames = self.buffer.extract(clip.start_idx, clip.end_idx);
            let saver = self.clip_saver.as_mut().unwrap();
            if let Err(e) = saver(&clip, frames) {
                self.clips.retain(|c| c != &clip);
                self.error(&format!("片段落盘失败: {e}"));
                return;
            }
        }
        self.speak(&prompt_saved(clip.seq), 0);
        self.transition(SmState::Ready, "saved", Some(clip));
        self.ready_elapsed = 0;
        self.countdown_next = (self.countdown_frames as f64 / self.fps).ceil() as i64 + 1;
    }

    fn transition(&mut self, next_state: SmState, reason: &str, clip: Option<Clip>) {
        let prev = self.state;
        self.state = next_state;
        let event = Transition {
            prev,
            next: next_state,
            reason: reason.to_string(),
            clip,
        };
        for f in &mut self.listeners {
            f(&event);
        }
    }

    fn speak(&mut self, text: &str, priority: i32) {
        if let Some(v) = self.voice.as_mut() {
            v.speak(text, priority);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::GrayFrame;
    use crate::voice::{NullVoice, PROMPT_DISCARDED, PROMPT_LEFT, PROMPT_READY, PROMPT_SWING, PROMPT_SWING_DONE};
    use std::sync::{Arc, Mutex};

    const FPS: f64 = 10.0;

    /// 可控就位检测器：present 由测试直接设置（对齐 Python conftest FakePresence）。
    #[derive(Default)]
    struct FakePresence {
        present: bool,
    }

    impl FakePresence {
        fn set(&mut self, present: bool) {
            self.present = present;
        }
    }

    impl PresenceDetect for FakePresence {
        fn update(&mut self, _frame: &GrayFrame) -> bool {
            self.present
        }
        fn present(&self) -> bool {
            self.present
        }
        fn reset(&mut self) {}
        fn last_ratio(&self) -> f64 {
            if self.present {
                1.0
            } else {
                0.0
            }
        }
    }

    /// 可控挥棒检测器：在指定帧号触发/结束（对齐 Python conftest FakeSwing）。
    struct FakeSwing {
        pre_roll: u64,
        start_at: Option<u64>,
        end_at: Option<u64>,
        active: bool,
        trigger_idx: i64,
    }

    impl FakeSwing {
        fn new(pre_roll: u64, start_at: Option<u64>, end_at: Option<u64>) -> Self {
            Self {
                pre_roll,
                start_at,
                end_at,
                active: false,
                trigger_idx: -1,
            }
        }
    }

    impl SwingDetect for FakeSwing {
        fn update(&mut self, _frame: &GrayFrame, frame_idx: u64) -> SwingEvent {
            if !self.active && self.start_at.is_some_and(|s| frame_idx >= s) {
                self.active = true;
                self.trigger_idx = frame_idx as i64;
                return SwingEvent::Started;
            }
            if self.active && self.end_at.is_some_and(|e| frame_idx >= e) {
                self.active = false;
                return SwingEvent::Ended;
            }
            SwingEvent::None
        }
        fn active(&self) -> bool {
            self.active
        }
        fn trigger_idx(&self) -> i64 {
            self.trigger_idx
        }
        fn pre_roll_frames(&self) -> u64 {
            self.pre_roll
        }
        fn set_manual_active(&mut self, active: bool, trigger_idx: i64) {
            self.active = active;
            if active {
                self.trigger_idx = trigger_idx;
            }
        }
        fn reset(&mut self) {
            self.active = false;
            self.trigger_idx = -1;
        }
        fn last_ratio(&self) -> f64 {
            0.0
        }
        fn last_energy(&self) -> f64 {
            0.0
        }
    }

    fn make_frame() -> GrayFrame {
        GrayFrame::new(200, 100, vec![0; 200 * 100])
    }

    /// 检测器/语音经 Arc<Mutex> 共享给状态机（等价 Python 引用语义，测试可在外部控制）。
    struct PresenceHandle(Arc<Mutex<FakePresence>>);
    impl PresenceDetect for PresenceHandle {
        fn update(&mut self, f: &GrayFrame) -> bool {
            self.0.lock().unwrap().update(f)
        }
        fn present(&self) -> bool {
            self.0.lock().unwrap().present()
        }
        fn reset(&mut self) {
            self.0.lock().unwrap().reset()
        }
        fn last_ratio(&self) -> f64 {
            self.0.lock().unwrap().last_ratio()
        }
    }

    struct SwingHandle(Arc<Mutex<FakeSwing>>);
    impl SwingDetect for SwingHandle {
        fn update(&mut self, f: &GrayFrame, idx: u64) -> SwingEvent {
            self.0.lock().unwrap().update(f, idx)
        }
        fn active(&self) -> bool {
            self.0.lock().unwrap().active()
        }
        fn trigger_idx(&self) -> i64 {
            self.0.lock().unwrap().trigger_idx()
        }
        fn pre_roll_frames(&self) -> u64 {
            self.0.lock().unwrap().pre_roll_frames()
        }
        fn set_manual_active(&mut self, active: bool, idx: i64) {
            self.0.lock().unwrap().set_manual_active(active, idx)
        }
        fn reset(&mut self) {
            self.0.lock().unwrap().reset()
        }
        fn last_ratio(&self) -> f64 {
            self.0.lock().unwrap().last_ratio()
        }
        fn last_energy(&self) -> f64 {
            self.0.lock().unwrap().last_energy()
        }
    }

    struct VoiceHandle(Arc<Mutex<NullVoice>>);
    impl Voice for VoiceHandle {
        fn speak(&mut self, text: &str, priority: i32) {
            self.0.lock().unwrap().speak(text, priority)
        }
        fn set_muted(&mut self, muted: bool) {
            self.0.lock().unwrap().set_muted(muted)
        }
    }

    type EventLog = Arc<Mutex<Vec<(SmState, SmState, String, Option<Clip>)>>>;
    type SavedLog = Arc<Mutex<Vec<(Clip, usize)>>>;

    /// 构建状态机夹具：(状态机, 就位控制柄, 挥棒控制柄, 语音, 迁移记录, 已保存片段)。
    fn build(
        swing: FakeSwing,
        countdown_seconds: f64,
        saver_fails: bool,
    ) -> (
        CaptureStateMachine,
        Arc<Mutex<FakePresence>>,
        Arc<Mutex<FakeSwing>>,
        Arc<Mutex<NullVoice>>,
        EventLog,
        SavedLog,
    ) {
        let presence = Arc::new(Mutex::new(FakePresence::default()));
        let swing = Arc::new(Mutex::new(swing));
        let voice = Arc::new(Mutex::new(NullVoice::new()));
        let events: EventLog = Arc::new(Mutex::new(Vec::new()));
        let saved: SavedLog = Arc::new(Mutex::new(Vec::new()));

        let events2 = Arc::clone(&events);
        let saved2 = Arc::clone(&saved);
        let saver: Option<ClipSaver> = Some(Box::new(move |clip: &Clip, frames: Vec<BufferItem>| {
            if saver_fails {
                return Err("磁盘已满".into());
            }
            saved2.lock().unwrap().push((clip.clone(), frames.len()));
            Ok(())
        }));

        let mut sm = CaptureStateMachine::new(
            Box::new(PresenceHandle(Arc::clone(&presence))),
            Box::new(SwingHandle(Arc::clone(&swing))),
            RingBuffer::new(3.0, FPS),
            FPS,
            Some(Box::new(VoiceHandle(Arc::clone(&voice)))),
            saver,
            countdown_seconds,
        );
        sm.add_listener(Box::new(move |t: &Transition| {
            events2
                .lock()
                .unwrap()
                .push((t.prev, t.next, t.reason.clone(), t.clip.clone()));
        }));
        (sm, presence, swing, voice, events, saved)
    }

    fn feed(sm: &mut CaptureStateMachine, start: u64, count: u64) -> u64 {
        for i in start..start + count {
            sm.feed_frame(i, i * 100_000_000, make_frame(), make_frame());
        }
        start + count
    }

    #[test]
    fn full_cycle_idle_to_ready() {
        let (mut sm, p, _, voice, events, _) = build(FakeSwing::new(2, None, None), 3.0, false);
        assert_eq!(sm.state, SmState::Idle);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 1);
        assert_eq!(sm.state, SmState::Ready);
        assert!(voice.lock().unwrap().texts().iter().any(|x| x == PROMPT_READY));
        let ev = events.lock().unwrap();
        assert_eq!(ev.last().unwrap().0, SmState::Idle);
        assert_eq!(ev.last().unwrap().1, SmState::Ready);
    }

    #[test]
    fn countdown_ticks_then_armed() {
        let (mut sm, p, _, voice, _, _) = build(FakeSwing::new(2, None, None), 3.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 31); // 30 帧倒计时 + 1
        assert_eq!(sm.state, SmState::Armed);
        let texts = voice.lock().unwrap().texts();
        for tick in ["3", "2", "1"] {
            assert!(texts.iter().any(|x| *x == tick));
        }
        assert!(texts.iter().any(|x| x == PROMPT_SWING));
        // 倒计时顺序：3 在 2 前，2 在 1 前，1 在请挥棒前
        let pos = |s: &str| texts.iter().position(|t| t.as_str() == s).unwrap();
        assert!(pos("3") < pos("2") && pos("2") < pos("1") && pos("1") < pos(PROMPT_SWING));
    }

    #[test]
    fn happy_path_produces_clip() {
        let (mut sm, p, _, voice, events, saved) =
            build(FakeSwing::new(5, Some(35), Some(45)), 3.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 50);
        assert_eq!(sm.state, SmState::Ready); // SAVING 后循环回 READY
        let saved = saved.lock().unwrap();
        assert_eq!(saved.len(), 1);
        let (clip, n_frames) = &saved[0];
        assert_eq!(clip.seq, 1);
        assert_eq!(clip.trigger_idx, 35);
        assert_eq!(clip.start_idx, 30); // trigger - pre_roll
        assert_eq!(clip.end_idx, 45);
        assert_eq!(*n_frames, 16);
        let texts = voice.lock().unwrap().texts();
        assert!(texts.iter().any(|x| x == PROMPT_SWING_DONE));
        let saved_prompt = prompt_saved(1);
        assert!(texts.iter().any(|x| *x == saved_prompt));
        let ev = events.lock().unwrap();
        let path: Vec<(SmState, SmState)> = ev.iter().map(|e| (e.0, e.1)).collect();
        assert!(path.contains(&(SmState::Armed, SmState::Swing)));
        assert!(path.contains(&(SmState::Swing, SmState::Saving)));
        assert!(path.contains(&(SmState::Saving, SmState::Ready)));
    }

    #[test]
    fn person_leaves_during_ready_goes_idle() {
        let (mut sm, p, _, voice, _, _) = build(FakeSwing::new(2, None, None), 3.0, false);
        p.lock().unwrap().set(true);
        let idx = feed(&mut sm, 0, 5);
        assert_eq!(sm.state, SmState::Ready);
        p.lock().unwrap().set(false);
        feed(&mut sm, idx, 1);
        assert_eq!(sm.state, SmState::Idle);
        assert!(voice.lock().unwrap().texts().iter().any(|x| x == PROMPT_LEFT));
    }

    #[test]
    fn person_leaves_during_swing_discards_clip() {
        let (mut sm, p, _, voice, _, saved) = build(FakeSwing::new(5, Some(35), Some(45)), 3.0, false);
        p.lock().unwrap().set(true);
        let idx = feed(&mut sm, 0, 40);
        assert_eq!(sm.state, SmState::Swing);
        p.lock().unwrap().set(false);
        feed(&mut sm, idx, 1);
        assert_eq!(sm.state, SmState::Idle);
        assert!(saved.lock().unwrap().is_empty()); // 中断不产出片段
        assert!(sm.clips.is_empty());
        assert!(voice.lock().unwrap().texts().iter().any(|x| x == PROMPT_LEFT));
    }

    #[test]
    fn discard_and_retake() {
        let (mut sm, p, s, voice, _, saved) = build(FakeSwing::new(5, Some(35), None), 3.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 40);
        assert_eq!(sm.state, SmState::Swing);
        sm.discard();
        assert_eq!(sm.state, SmState::Ready);
        assert!(saved.lock().unwrap().is_empty());
        assert!(voice.lock().unwrap().texts().iter().any(|x| x == PROMPT_DISCARDED));
        // 重拍：倒计时后再次 ARMED → 触发 → 结束 → 产出
        {
            let mut sw = s.lock().unwrap();
            sw.start_at = Some(75);
            sw.end_at = Some(85);
        }
        feed(&mut sm, 40, 50);
        assert_eq!(saved.lock().unwrap().len(), 1);
        assert_eq!(saved.lock().unwrap()[0].0.seq, 1);
        assert_eq!(sm.state, SmState::Ready);
    }

    #[test]
    fn manual_start_stop_fallback() {
        let (mut sm, p, _, _, _, saved) = build(FakeSwing::new(2, None, None), 1.0, false);
        p.lock().unwrap().set(true);
        let idx = feed(&mut sm, 0, 11); // 10 帧倒计时 → ARMED
        assert_eq!(sm.state, SmState::Armed);
        sm.manual_start(None);
        assert_eq!(sm.state, SmState::Swing);
        feed(&mut sm, idx, 5);
        sm.manual_stop(None);
        assert_eq!(sm.state, SmState::Ready);
        let saved = saved.lock().unwrap();
        assert_eq!(saved.len(), 1);
        assert!(saved[0].0.end_idx >= saved[0].0.start_idx);
    }

    #[test]
    fn error_and_recover() {
        let (mut sm, p, _, _, _, _) = build(FakeSwing::new(2, None, None), 3.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 1);
        sm.error("相机断开");
        assert_eq!(sm.state, SmState::Error);
        assert_eq!(sm.last_error.as_deref(), Some("相机断开"));
        // ERROR 态忽略帧
        feed(&mut sm, 1, 10);
        assert_eq!(sm.state, SmState::Error);
        sm.recover();
        assert_eq!(sm.state, SmState::Idle);
    }

    #[test]
    fn clip_saver_failure_goes_error() {
        let (mut sm, p, _, _, _, _) = build(FakeSwing::new(2, Some(35), Some(40)), 1.0, true);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 45);
        assert_eq!(sm.state, SmState::Error);
        assert!(sm.last_error.unwrap_or_default().contains("磁盘已满"));
        assert!(sm.clips.is_empty()); // 失败片段被回滚
    }

    #[test]
    fn pause_from_working_state() {
        let (mut sm, p, _, _, _, _) = build(FakeSwing::new(2, None, None), 3.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 5);
        sm.pause();
        assert_eq!(sm.state, SmState::Idle);
    }

    #[test]
    fn pre_roll_clamped_to_buffer() {
        // 缓冲只够 1s（10 帧），pre_roll 8 帧但早期帧已被覆盖
        let (mut sm, p, _, _, _, saved) = build(FakeSwing::new(8, Some(12), Some(15)), 0.2, false);
        sm.buffer = RingBuffer::new(1.0, FPS); // 容量 10
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 16); // idx 15 结束挥棒；不再喂帧，避免 FakeSwing 阈值重复触发
        let saved = saved.lock().unwrap();
        assert_eq!(saved.len(), 1);
        let (clip, n_frames) = &saved[0];
        // trigger=12，pre_roll=8 → 期望 4，但容量 10 的缓冲只覆盖 [6,15]，钳到 6
        assert_eq!(clip.start_idx, 6);
        assert_eq!(*n_frames, 10); // 受缓冲容量限制
    }

    #[test]
    fn listener_receives_all_transitions() {
        let (mut sm, p, _, _, events, _) = build(FakeSwing::new(2, Some(15), Some(20)), 1.0, false);
        p.lock().unwrap().set(true);
        feed(&mut sm, 0, 25);
        let ev = events.lock().unwrap();
        let reasons: Vec<&str> = ev.iter().map(|e| e.2.as_str()).collect();
        assert_eq!(reasons[0], "presence_settled");
        assert!(reasons.contains(&"countdown_done"));
        assert!(reasons.contains(&"swing_started"));
        assert!(reasons.contains(&"swing_ended"));
        assert!(reasons.contains(&"saved"));
        let saved_events: Vec<_> = ev.iter().filter(|e| e.2 == "saved").collect();
        assert!(saved_events[0].3.is_some());
    }
}
