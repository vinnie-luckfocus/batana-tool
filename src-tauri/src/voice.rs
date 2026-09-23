//! 语音引导：macOS `say` 命令播报（PRD F4）。
//! 语义对齐 Python 版 app/voice/（voice.py 文案 + say_voice.py 实现）。

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::process::Command;
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use crate::capture::find_in_path;

// ---- 中文提示文案（PRD F4，与 Python 版一致，勿改文案） ----
pub const PROMPT_READY: &str = "已就位，请准备";
pub const PROMPT_SWING: &str = "请挥棒";
pub const PROMPT_SWING_DONE: &str = "挥棒完成";
pub const PROMPT_LEFT: &str = "人员离开，已暂停采集";
pub const PROMPT_ERROR: &str = "出现异常，请检查设备";
pub const PROMPT_ERROR_CAMERA: &str = "相机断开，请检查设备连接";
pub const PROMPT_ERROR_STORAGE: &str = "存储失败，请检查磁盘空间";
pub const PROMPT_DISCARDED: &str = "已丢弃，请重新挥棒";

/// 异常文案分类关键词（消息小写后匹配；存储优先于相机）
const STORAGE_KEYWORDS: [&str; 8] = ["存储", "落盘", "写入", "磁盘", "空间", "disk", "write", "storage"];
const CAMERA_KEYWORDS: [&str; 7] = ["相机", "camera", "uvc", "设备", "打开", "断开", "读帧"];

/// 按异常消息分类语音提示：相机断开 / 存储失败 / 通用异常。
pub fn error_prompt(message: &str) -> &'static str {
    let msg = message.to_lowercase();
    if STORAGE_KEYWORDS.iter().any(|k| msg.contains(k)) {
        return PROMPT_ERROR_STORAGE;
    }
    if CAMERA_KEYWORDS.iter().any(|k| msg.contains(k)) {
        return PROMPT_ERROR_CAMERA;
    }
    PROMPT_ERROR
}

pub fn prompt_saved(seq: u64) -> String {
    format!("已保存，第 {seq} 段，请准备下一段")
}

/// TTS 抽象：异步播报，永不阻塞采集流程。
pub trait Voice: Send {
    /// 播报文本；priority 越大越优先（默认 0）。
    fn speak(&mut self, text: &str, priority: i32);
    fn set_muted(&mut self, muted: bool);
}

/// 优先级队列元素：priority 大者优先，同优先级按入队序（seq 小者优先）。
struct Item {
    priority: i32,
    seq: u64,
    text: String,
}

impl PartialEq for Item {
    fn eq(&self, other: &Self) -> bool {
        self.priority == other.priority && self.seq == other.seq
    }
}
impl Eq for Item {}
impl PartialOrd for Item {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Item {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.priority
            .cmp(&other.priority)
            .then(Reverse(self.seq).cmp(&Reverse(other.seq)))
    }
}

/// 调用 macOS `say -v Tingting` 播报中文。
///
/// 内部单 worker 线程串行消费优先级队列，语音之间不重叠、不打断采集；
/// 播报进程异常只记录，不向外抛。
pub struct SayVoice {
    shared: std::sync::Arc<Shared>,
    worker: Option<std::thread::JoinHandle<()>>,
}

struct Shared {
    inner: Mutex<Inner>,
    wake: Condvar,
}

struct Inner {
    heap: BinaryHeap<Item>,
    seq: u64,
    muted: bool,
    closed: bool,
}

impl SayVoice {
    pub fn new(voice: &str, rate: Option<u32>) -> Result<Self, String> {
        if find_in_path("say").is_none() {
            return Err("未找到 macOS `say` 命令（本实现仅支持 macOS）".into());
        }
        let shared = std::sync::Arc::new(Shared {
            inner: Mutex::new(Inner {
                heap: BinaryHeap::new(),
                seq: 0,
                muted: false,
                closed: false,
            }),
            wake: Condvar::new(),
        });
        let voice = voice.to_string();
        let shared2 = std::sync::Arc::clone(&shared);
        let worker_voice = voice.clone();
        let worker = std::thread::Builder::new()
            .name("say-voice".into())
            .spawn(move || {
                loop {
                    let item = {
                        let mut g = shared2.inner.lock().unwrap();
                        loop {
                            if g.closed {
                                return;
                            }
                            if let Some(item) = g.heap.pop() {
                                break item;
                            }
                            let (guard, _) = shared2
                                .wake
                                .wait_timeout(g, Duration::from_millis(100))
                                .unwrap();
                            g = guard;
                        }
                    };
                    let mut cmd = Command::new("say");
                    cmd.arg("-v").arg(&worker_voice);
                    if let Some(r) = rate {
                        cmd.arg("-r").arg(r.to_string());
                    }
                    cmd.arg(&item.text);
                    let _ = cmd.status(); // 播报失败不阻断采集
                }
            })
            .map_err(|e| format!("启动语音线程失败: {e}"))?;
        let _ = voice;
        Ok(Self {
            shared,
            worker: Some(worker),
        })
    }

    pub fn new_default(rate: Option<u32>) -> Result<Self, String> {
        Self::new("Tingting", rate)
    }
}

impl Voice for SayVoice {
    fn speak(&mut self, text: &str, priority: i32) {
        let mut g = self.shared.inner.lock().unwrap();
        if g.muted || g.closed {
            return;
        }
        g.seq += 1;
        let seq = g.seq;
        g.heap.push(Item {
            priority,
            seq,
            text: text.to_string(),
        });
        drop(g);
        self.shared.wake.notify_one();
    }

    fn set_muted(&mut self, muted: bool) {
        let mut g = self.shared.inner.lock().unwrap();
        g.muted = muted;
        if muted {
            g.heap.clear();
        }
    }
}

impl Drop for SayVoice {
    fn drop(&mut self) {
        {
            let mut g = self.shared.inner.lock().unwrap();
            g.closed = true;
        }
        self.shared.wake.notify_one();
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// 测试用语音：不发声，只记录播报历史。
#[derive(Default)]
pub struct NullVoice {
    pub history: Vec<(String, i32)>,
    pub muted: bool,
}

impl NullVoice {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn texts(&self) -> Vec<String> {
        self.history.iter().map(|(t, _)| t.clone()).collect()
    }
}

impl Voice for NullVoice {
    fn speak(&mut self, text: &str, priority: i32) {
        if !self.muted {
            self.history.push((text.to_string(), priority));
        }
    }

    fn set_muted(&mut self, muted: bool) {
        self.muted = muted;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_prompt_classification() {
        assert_eq!(error_prompt("磁盘已满，写入失败"), PROMPT_ERROR_STORAGE);
        assert_eq!(error_prompt("disk full"), PROMPT_ERROR_STORAGE);
        assert_eq!(error_prompt("相机断开"), PROMPT_ERROR_CAMERA);
        assert_eq!(error_prompt("camera disconnected"), PROMPT_ERROR_CAMERA);
        assert_eq!(error_prompt("未知问题"), PROMPT_ERROR);
        // 存储优先于相机
        assert_eq!(error_prompt("相机存储写入失败"), PROMPT_ERROR_STORAGE);
    }

    #[test]
    fn prompt_saved_format() {
        assert_eq!(prompt_saved(1), "已保存，第 1 段，请准备下一段");
    }

    #[test]
    fn null_voice_records_history_and_mute() {
        let mut v = NullVoice::new();
        v.speak(PROMPT_READY, 0);
        v.set_muted(true);
        v.speak(PROMPT_SWING, 0);
        assert_eq!(v.texts(), vec![PROMPT_READY.to_string()]);
    }
}
