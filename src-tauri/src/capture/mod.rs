//! 采集层：ffmpeg avfoundation 帧源、SBS 切分、环形缓冲、设备枚举、采集编排命令。
//!
//! 线程模型（对齐 Python 版 app/ui/controller.py，改用 std::thread + mpsc）：
//!     采集线程：ffmpeg 管道读帧 → SBS 切分左右目 → 状态机 feed_frame
//!     落盘线程：片段编码/写盘串行消费（不阻塞抓帧）
//!     控制命令经 mpsc 注入采集线程；事件经 tauri::ipc::Channel 推送前端。

pub mod clip_writer;
pub mod devices;
pub mod ffmpeg_source;
pub mod ring;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Instant;

use serde::Serialize;
use tauri::ipc::Channel;

use crate::detect::{PresenceDetector, PresenceMethod, SwingDetector};
use crate::session::settings::AppSettings;
use crate::session::store::SessionStore;
use crate::state::{CaptureStateMachine, Clip};
use crate::voice::{SayVoice, Voice};

pub use devices::{list_video_devices, parse_device_list, pick_default, VideoDevice};
pub use ffmpeg_source::{yuyv_to_gray, FfmpegUvcSource};
pub use ring::RingBuffer;

/// ROI = (x, y, w, h)，左目像素坐标
pub type Roi = (usize, usize, usize, usize);

/// 预览降频目标（PRD：120fps 流下预览 30fps 显示）
const PREVIEW_FPS: f64 = 30.0;
/// 预览分辨率（左目缩略图）
const PREVIEW_W: usize = 320;
const PREVIEW_H: usize = 200;

/// 灰度帧（MONO8）。data 用 Arc 共享，环形缓冲/片段提取克隆零拷贝。
#[derive(Debug, Clone)]
pub struct GrayFrame {
    pub width: usize,
    pub height: usize,
    pub data: Arc<Vec<u8>>,
}

impl GrayFrame {
    pub fn new(width: usize, height: usize, data: Vec<u8>) -> Self {
        assert_eq!(data.len(), width * height, "帧数据长度与尺寸不符");
        Self {
            width,
            height,
            data: Arc::new(data),
        }
    }

    pub fn at(&self, x: usize, y: usize) -> u8 {
        self.data[y * self.width + x]
    }
}

/// 缓冲元素 = (帧序号, 时间戳 ns, 左目帧, 右目帧)
pub type BufferItem = (u64, u64, GrayFrame, GrayFrame);

/// 在 PATH 中查找可执行文件（等价 shutil.which）。
pub fn find_in_path(exe: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let p = dir.join(exe);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

/// SBS（side-by-side）双目整帧对半切为 (左目, 右目)。帧宽必须为偶数。
pub fn split_sbs(frame: &GrayFrame) -> Result<(GrayFrame, GrayFrame), String> {
    let width = frame.width;
    if width % 2 != 0 {
        return Err(format!("SBS 帧宽必须为偶数，实际: {width}"));
    }
    let mid = width / 2;
    let mut left = Vec::with_capacity(mid * frame.height);
    let mut right = Vec::with_capacity(mid * frame.height);
    for row in 0..frame.height {
        let start = row * width;
        left.extend_from_slice(&frame.data[start..start + mid]);
        right.extend_from_slice(&frame.data[start + mid..start + width]);
    }
    Ok((
        GrayFrame::new(mid, frame.height, left),
        GrayFrame::new(mid, frame.height, right),
    ))
}

// ---- 推送前端的事件（字段 snake_case，serde 内部标签） ----

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CaptureEvent {
    /// 预览帧：左目 320x200 RGBA 原始字节（已降频 ≤30fps）
    Preview { width: u32, height: u32, rgba: Vec<u8> },
    /// 遥测：实测帧率 / 就位占比(0-1) / 运动占比(0-1) / 状态名
    Telemetry {
        fps: f64,
        presence_ratio: f64,
        motion_ratio: f64,
        state: String,
    },
    /// 状态迁移
    StateChanged {
        prev: String,
        next: String,
        reason: String,
        seq: Option<u64>,
    },
    /// 片段保存完成
    ClipSaved {
        session_id: String,
        seq: u64,
        dir: String,
        frame_count: u64,
    },
    /// 错误（相机断开 / 落盘失败等）
    Error { message: String },
}

fn emit(channel: &Channel<serde_json::Value>, ev: &CaptureEvent) {
    if let Ok(value) = serde_json::to_value(ev) {
        let _ = channel.send(value);
    }
}

// ---- 应用状态与采集句柄 ----

/// 注入采集线程的控制命令。
pub enum CaptureCommand {
    ManualToggle,
    Discard,
    SetMuted(bool),
}

pub struct CaptureHandle {
    stop: Arc<AtomicBool>,
    cmd_tx: mpsc::Sender<CaptureCommand>,
    state_label: Arc<Mutex<String>>,
    join: Option<JoinHandle<()>>,
}

pub struct AppState {
    pub settings_path: PathBuf,
    pub settings: AppSettings,
    pub capture: Option<CaptureHandle>,
}

impl AppState {
    pub fn load() -> Self {
        let path = crate::session::settings::default_settings_path();
        Self::with_path(path)
    }

    pub fn with_path(settings_path: PathBuf) -> Self {
        let settings = AppSettings::load(&settings_path);
        Self {
            settings_path,
            settings,
            capture: None,
        }
    }
}

// ---- 落盘线程 ----

/// 落盘任务：片段元数据 + 已提取帧序列（提取在采集线程完成，避免环形缓冲被覆盖）。
struct SaveJob {
    seq: u64,
    /// 段内相对触发帧号（trigger_idx - start_idx），对齐 Python controller 语义
    trigger_rel: u64,
    items: Vec<BufferItem>,
}

/// 片段保存完成信息。
struct ClipSavedInfo {
    session_id: String,
    seq: u64,
    dir: String,
    frame_count: u64,
}

fn run_saver(
    settings: AppSettings,
    job_rx: mpsc::Receiver<SaveJob>,
    result_tx: mpsc::Sender<Result<ClipSavedInfo, String>>,
) {
    let store = match SessionStore::new(&settings.storage_root) {
        Ok(s) => s,
        Err(e) => {
            let _ = result_tx.send(Err(format!("打开素材库失败: {e}")));
            return;
        }
    };
    let mut store = store;
    while let Ok(job) = job_rx.recv() {
        let result = (|| -> Result<ClipSavedInfo, String> {
            let roi = settings.roi.map(|r| serde_json::json!(r));
            let out_dir = crate::session::export::export_session(
                &job.items,
                std::path::Path::new(&settings.storage_root),
                settings.capture_fps,
                crate::session::export::ExportOptions {
                    capture_extra: Some(serde_json::json!({
                        "source": "ui-capture",
                        "roi": roi,
                    })),
                    ..Default::default()
                },
            )?;
            let session_id = out_dir
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            store.add_clip(
                &session_id,
                &out_dir,
                job.items.len() as u64,
                settings.capture_fps,
                Some(job.trigger_rel),
                None,
            )?;
            Ok(ClipSavedInfo {
                session_id,
                seq: job.seq,
                dir: out_dir.to_string_lossy().into_owned(),
                frame_count: job.items.len() as u64,
            })
        })();
        if result_tx.send(result).is_err() {
            return; // 采集线程已退出
        }
    }
}

// ---- 采集线程 ----

fn run_capture(
    settings: AppSettings,
    channel: Channel<serde_json::Value>,
    cmd_rx: mpsc::Receiver<CaptureCommand>,
    stop: Arc<AtomicBool>,
    state_label: Arc<Mutex<String>>,
) {
    if let Err(e) = run_capture_inner(&settings, &channel, &cmd_rx, &stop, &state_label) {
        emit(&channel, &CaptureEvent::Error { message: e });
    }
    *state_label.lock().unwrap() = "STOPPED".into();
}

fn run_capture_inner(
    settings: &AppSettings,
    channel: &Channel<serde_json::Value>,
    cmd_rx: &mpsc::Receiver<CaptureCommand>,
    stop: &Arc<AtomicBool>,
    state_label: &Arc<Mutex<String>>,
) -> Result<(), String> {
    let (w, h, fps) = (
        settings.capture_width as usize,
        settings.capture_height as usize,
        settings.capture_fps,
    );
    let mut source = FfmpegUvcSource::new(&settings.camera_name, w, h, fps)?;
    let eye_w = w / 2;
    let eye_h = h;

    // 默认 ROI：对齐 Python controller 首帧懒建 (w//4, h//8, w//2, h*3//4)
    let roi = settings
        .roi_tuple()
        .unwrap_or((eye_w / 4, eye_h / 8, eye_w / 2, eye_h * 3 / 4));

    let presence = PresenceDetector::new(
        roi,
        fps,
        settings.presence_ratio,
        1.0,
        1.0,
        PresenceMethod::BgMean,
        0.002,
        0.08,
    );
    let swing = SwingDetector::new(
        roi,
        fps,
        settings.motion_pix_thresh,
        settings.motion_trigger_pct / 100.0,
        settings.motion_release_pct / 100.0,
        settings.pre_roll_seconds,
        settings.post_roll_seconds,
    );

    let voice: Option<Box<dyn Voice>> = if settings.voice_enabled {
        SayVoice::new_default(Some(settings.voice_rate))
            .ok()
            .map(|v| Box::new(v) as Box<dyn Voice>)
    } else {
        None
    };

    // 落盘线程：抓帧线程只入队，编码/写盘串行消费不阻塞采集（对齐 Python _SaveWorker）
    let (job_tx, job_rx) = mpsc::channel::<SaveJob>();
    let (result_tx, result_rx) = mpsc::channel::<Result<ClipSavedInfo, String>>();
    let saver_settings = settings.clone();
    let saver = std::thread::Builder::new()
        .name("clip-saver".into())
        .spawn(move || run_saver(saver_settings, job_rx, result_tx))
        .map_err(|e| format!("启动落盘线程失败: {e}"))?;

    let clip_saver = Box::new(move |_clip: &Clip, frames: Vec<BufferItem>| {
        // 帧序列由状态机从环形缓冲提取完毕；入队即返回，不阻塞抓帧
        let trigger_rel = _clip.trigger_idx.saturating_sub(_clip.start_idx);
        job_tx
            .send(SaveJob {
                seq: _clip.seq,
                trigger_rel,
                items: frames,
            })
            .map_err(|_| "落盘线程已退出".to_string())
    });

    let mut sm = CaptureStateMachine::new(
        Box::new(presence),
        Box::new(swing),
        RingBuffer::new(settings.buffer_seconds, fps),
        fps,
        voice,
        Some(clip_saver),
        settings.countdown_seconds,
    );

    // 状态迁移 → 前端事件 + 共享状态标签
    {
        let ch = channel.clone();
        let label = Arc::clone(state_label);
        sm.add_listener(Box::new(move |t: &crate::state::Transition| {
            *label.lock().unwrap() = t.next.as_str().to_string();
            emit(
                &ch,
                &CaptureEvent::StateChanged {
                    prev: t.prev.as_str().to_string(),
                    next: t.next.as_str().to_string(),
                    reason: t.reason.clone(),
                    seq: t.clip.as_ref().map(|c| c.seq),
                },
            );
        }));
    }

    let preview_stride = ((fps / PREVIEW_FPS) + 0.5).max(1.0) as u64;
    let telemetry_stride = ((fps / 10.0) + 0.5).max(1.0) as u64;
    let t0 = Instant::now();
    let mut n_frames = 0u64;

    loop {
        if stop.load(Ordering::Relaxed) {
            break;
        }
        // 控制命令（手动开始/结束、丢弃）
        while let Ok(cmd) = cmd_rx.try_recv() {
            match cmd {
                CaptureCommand::ManualToggle => {
                    sm.manual_toggle();
                }
                CaptureCommand::Discard => sm.discard(),
                CaptureCommand::SetMuted(muted) => sm.set_muted(muted),
            }
        }
        // 落盘结果回填
        while let Ok(result) = result_rx.try_recv() {
            match result {
                Ok(info) => emit(
                    channel,
                    &CaptureEvent::ClipSaved {
                        session_id: info.session_id,
                        seq: info.seq,
                        dir: info.dir,
                        frame_count: info.frame_count,
                    },
                ),
                Err(msg) => sm.error(&format!("片段落盘失败: {msg}")),
            }
        }

        let (idx, ts_ns, sbs) = match source.next_frame() {
            Ok(f) => f,
            Err(e) => {
                sm.error(&e); // 语音 + StateChanged(ERROR)
                return Err(e); // 再上抛 Error 事件并退出线程
            }
        };
        let (left, right) = split_sbs(&sbs)?;
        sm.feed_frame(idx, ts_ns, left.clone(), right);

        n_frames += 1;
        let elapsed = t0.elapsed().as_secs_f64();
        let measured_fps = if elapsed > 0.0 {
            n_frames as f64 / elapsed
        } else {
            0.0
        };

        if idx % preview_stride == 0 {
            let (pw, ph, rgba) = preview_rgba(&left);
            emit(
                channel,
                &CaptureEvent::Preview {
                    width: pw,
                    height: ph,
                    rgba,
                },
            );
        }
        if idx % telemetry_stride == 0 {
            emit(
                channel,
                &CaptureEvent::Telemetry {
                    fps: measured_fps,
                    presence_ratio: sm.presence_ratio(),
                    motion_ratio: sm.motion_ratio(),
                    state: sm.state.as_str().to_string(),
                },
            );
        }
    }

    // 关停：先丢状态机（关闭落盘通道），再排空落盘队列，不丢片段
    drop(sm);
    source.close();
    let _ = saver.join();
    Ok(())
}

/// 左目灰度 → 320x200 RGBA 缩略图（最近邻采样）。
fn preview_rgba(frame: &GrayFrame) -> (u32, u32, Vec<u8>) {
    let mut rgba = Vec::with_capacity(PREVIEW_W * PREVIEW_H * 4);
    for y in 0..PREVIEW_H {
        let sy = y * frame.height / PREVIEW_H;
        for x in 0..PREVIEW_W {
            let sx = x * frame.width / PREVIEW_W;
            let v = frame.at(sx, sy);
            rgba.extend_from_slice(&[v, v, v, 255]);
        }
    }
    (PREVIEW_W as u32, PREVIEW_H as u32, rgba)
}

// ---- Tauri 命令 ----

#[tauri::command]
pub fn list_devices() -> Vec<VideoDevice> {
    list_video_devices()
}

#[tauri::command]
pub fn start_capture(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    channel: Channel<serde_json::Value>,
) -> Result<(), String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    if st.capture.is_some() {
        return Err("采集中，请先停止".into());
    }
    let settings = st.settings.clone();
    let (cmd_tx, cmd_rx) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    let state_label = Arc::new(Mutex::new("IDLE".to_string()));
    let join = {
        let stop2 = Arc::clone(&stop);
        let label2 = Arc::clone(&state_label);
        std::thread::Builder::new()
            .name("capture".into())
            .spawn(move || run_capture(settings, channel, cmd_rx, stop2, label2))
            .map_err(|e| format!("启动采集线程失败: {e}"))?
    };
    st.capture = Some(CaptureHandle {
        stop,
        cmd_tx,
        state_label,
        join: Some(join),
    });
    Ok(())
}

#[tauri::command]
pub fn stop_capture(state: tauri::State<'_, Arc<Mutex<AppState>>>) -> Result<(), String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let Some(mut handle) = st.capture.take() else {
        return Ok(()); // 未在采集，幂等
    };
    handle.stop.store(true, Ordering::Relaxed);
    if let Some(join) = handle.join.take() {
        // 帧读取有 10s 断流超时，极端情况下 join 最长阻塞一个超时周期
        let _ = join.join();
    }
    Ok(())
}

#[tauri::command]
pub fn discard_clip(state: tauri::State<'_, Arc<Mutex<AppState>>>) -> Result<(), String> {
    let st = state.lock().map_err(|e| e.to_string())?;
    let Some(handle) = &st.capture else {
        return Err("未在采集".into());
    };
    handle
        .cmd_tx
        .send(CaptureCommand::Discard)
        .map_err(|_| "采集线程已退出".to_string())
}

/// 手动开始/结束（兜底，PRD F5）：ARMED→SWING / SWING→SAVING。
/// 返回指令排队时的状态机状态标签。
#[tauri::command]
pub fn manual_toggle(state: tauri::State<'_, Arc<Mutex<AppState>>>) -> Result<String, String> {
    let st = state.lock().map_err(|e| e.to_string())?;
    let Some(handle) = &st.capture else {
        return Err("未在采集".into());
    };
    let label = handle.state_label.lock().map_err(|e| e.to_string())?.clone();
    handle
        .cmd_tx
        .send(CaptureCommand::ManualToggle)
        .map_err(|_| "采集线程已退出".to_string())?;
    Ok(label)
}

/// 语音静音即时切换（采集线程内生效）；未在采集时幂等返回 Ok。
#[tauri::command]
pub fn set_muted(state: tauri::State<'_, Arc<Mutex<AppState>>>, muted: bool) -> Result<(), String> {
    let st = state.lock().map_err(|e| e.to_string())?;
    if let Some(handle) = &st.capture {
        handle
            .cmd_tx
            .send(CaptureCommand::SetMuted(muted))
            .map_err(|_| "采集线程已退出".to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_sbs_halves_frame() {
        // 4x2 帧：左半 [1,2]，右半 [3,4]
        let data = vec![1, 2, 3, 4, 5, 6, 7, 8];
        let frame = GrayFrame::new(4, 2, data);
        let (l, r) = split_sbs(&frame).unwrap();
        assert_eq!((l.width, l.height), (2, 2));
        assert_eq!(&l.data[..], &[1, 2, 5, 6]);
        assert_eq!(&r.data[..], &[3, 4, 7, 8]);
    }

    #[test]
    fn split_sbs_odd_width_rejected() {
        let frame = GrayFrame::new(3, 2, vec![0; 6]);
        assert!(split_sbs(&frame).unwrap_err().contains("偶数"));
    }

    #[test]
    fn preview_rgba_dimensions() {
        let frame = GrayFrame::new(1280, 400, vec![128; 1280 * 400]);
        let (w, h, rgba) = preview_rgba(&frame);
        assert_eq!((w, h), (320, 200));
        assert_eq!(rgba.len(), 320 * 200 * 4);
        assert_eq!(&rgba[..4], &[128, 128, 128, 255]);
    }

    #[test]
    fn capture_event_serializes_snake_case_tagged() {
        let v = serde_json::to_value(&CaptureEvent::Telemetry {
            fps: 75.0,
            presence_ratio: 0.5,
            motion_ratio: 0.02,
            state: "ARMED".into(),
        })
        .unwrap();
        assert_eq!(v["type"], "telemetry");
        assert!(v.get("presence_ratio").is_some());
        assert!(v.get("motion_ratio").is_some());
        let v2 = serde_json::to_value(&CaptureEvent::StateChanged {
            prev: "IDLE".into(),
            next: "READY".into(),
            reason: "presence_settled".into(),
            seq: None,
        })
        .unwrap();
        assert_eq!(v2["type"], "state_changed");
        assert_eq!(v2["seq"], serde_json::Value::Null);
    }
}
