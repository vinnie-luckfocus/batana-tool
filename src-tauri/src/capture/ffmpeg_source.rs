//! ffmpeg AVFoundation 帧源：macOS 上直通 AVCaptureDeviceFormat 选定双目模组模式。
//! 语义对齐 Python 版 app/capture/ffmpeg_source.py。
//!
//! 帧读取：ffmpeg 输出 rawvideo（yuyv422，每帧 width*height*2 字节）到管道，
//! 每像素组首字节即亮度 Y，直接得到 MONO8 灰度帧。
//!
//! 与 Python 版的差异：Rust std 无 select/poll，改为独立读线程组帧后经 mpsc 整帧转发，
//! recv_timeout 实现整体 10s 超时（超时按断流处理）。

use std::io::Read;
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use super::{find_in_path, GrayFrame};

pub const DEFAULT_DEVICE_NAME: &str = "USB Global Camera";
/// 读帧整体超时（秒）：超时（如相机权限被拒导致无帧）按断流处理
const READ_TIMEOUT: Duration = Duration::from_secs(10);

/// 进程级单调时钟起点（对齐 Python time.monotonic_ns() 的单调语义）
fn monotonic_ns() -> u64 {
    use std::sync::OnceLock;
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_nanos() as u64
}

/// ffmpeg avfoundation 双目帧源（macOS 专用）。
pub struct FfmpegUvcSource {
    pub device: String,
    pub width: usize,
    pub height: usize,
    pub fps: f64,
    frame_bytes: usize,
    child: Child,
    stderr: Option<ChildStderr>,
    rx: mpsc::Receiver<std::io::Result<Vec<u8>>>,
    primed: Option<Vec<u8>>,
    idx: u64,
}

impl FfmpegUvcSource {
    /// 构造即预热：同步读首帧——模式不被支持时 ffmpeg 立即退出（EOF），
    /// 让调用方在构造期报错，而不是采集中途才报错。
    pub fn new(device: &str, width: usize, height: usize, fps: f64) -> Result<Self, String> {
        if !cfg!(target_os = "macos") {
            return Err("FfmpegUvcSource 仅支持 macOS（avfoundation）".into());
        }
        let ffmpeg = find_in_path("ffmpeg").ok_or("未找到 ffmpeg（brew install ffmpeg）")?;
        let frame_bytes = width * height * 2; // yuyv422
        let mut child = Command::new(ffmpeg)
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "avfoundation",
                "-pixel_format",
                "yuyv422",
                "-framerate",
                &format!("{}", fps as u64),
                "-video_size",
                &format!("{width}x{height}"),
                "-i",
                &format!("{device}:none"),
                "-f",
                "rawvideo",
                "pipe:1",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("启动 ffmpeg 失败: {e}"))?;

        let stdout: ChildStdout = child.stdout.take().expect("stdout 已管道化");
        let (tx, rx) = mpsc::channel();
        // 读线程：阻塞读管道，组帧后整帧转发（避免消费端逐块 drain/collect 的额外拷贝）；
        // EOF/出错后通道断开
        thread::Builder::new()
            .name("ffmpeg-reader".into())
            .spawn(move || {
                let mut stdout = stdout;
                let mut frame = vec![0u8; frame_bytes];
                let mut filled = 0usize;
                loop {
                    match stdout.read(&mut frame[filled..]) {
                        Ok(0) => break,
                        Ok(n) => {
                            filled += n;
                            if filled == frame_bytes {
                                if tx.send(Ok(std::mem::take(&mut frame))).is_err() {
                                    break; // 接收方已退出（停止采集）
                                }
                                frame = vec![0u8; frame_bytes];
                                filled = 0;
                            }
                        }
                        Err(e) => {
                            let _ = tx.send(Err(e));
                            break;
                        }
                    }
                }
            })
            .map_err(|e| format!("启动读线程失败: {e}"))?;

        let mut src = Self {
            device: device.to_string(),
            width,
            height,
            fps,
            frame_bytes,
            child,
            stderr: None,
            rx,
            primed: None,
            idx: 0,
        };
        src.stderr = src.child.stderr.take();
        let first = src.recv_frame();
        if first.len() < frame_bytes {
            return Err(src.fail(&format!(
                "ffmpeg 未能打开设备 {device:?} 或模式 {width}x{height}@{fps:.0} 不被支持"
            )));
        }
        src.primed = Some(first);
        Ok(src)
    }

    /// ffmpeg 可用性：macOS 且 PATH 中存在 ffmpeg。
    pub fn available() -> bool {
        cfg!(target_os = "macos") && find_in_path("ffmpeg").is_some()
    }

    /// 收一整帧；超时/断流/读线程出错返回空缓冲（调用方按失败处理）。
    fn recv_frame(&mut self) -> Vec<u8> {
        match self.rx.recv_timeout(READ_TIMEOUT) {
            Ok(Ok(frame)) => frame,
            _ => Vec::new(),
        }
    }

    /// 先终止进程再读 stderr，否则进程存活时 stderr 读取永久阻塞。
    fn fail(&mut self, msg: &str) -> String {
        self.close();
        let mut detail = String::new();
        if let Some(mut err) = self.stderr.take() {
            let mut buf = String::new();
            if err.read_to_string(&mut buf).is_ok() {
                detail = buf.trim().to_string();
            }
        }
        if detail.is_empty() {
            msg.to_string()
        } else {
            format!("{msg}：{detail}")
        }
    }

    /// 读取下一帧（含预热首帧），返回 (帧序号, 单调时间戳 ns, 灰度帧)。
    pub fn next_frame(&mut self) -> Result<(u64, u64, GrayFrame), String> {
        let buf = match self.primed.take() {
            Some(b) => b,
            None => {
                let b = self.recv_frame();
                if b.len() < self.frame_bytes {
                    return Err(self.fail("采集中断（设备可能已断开）"));
                }
                b
            }
        };
        let idx = self.idx;
        self.idx += 1;
        Ok((
            idx,
            monotonic_ns(),
            yuyv_to_gray(&buf, self.width, self.height),
        ))
    }

    pub fn close(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for FfmpegUvcSource {
    fn drop(&mut self) {
        self.close();
    }
}

/// yuyv422 原始字节 → MONO8（取每像素组首字节 Y 通道）。
pub fn yuyv_to_gray(buf: &[u8], width: usize, height: usize) -> GrayFrame {
    let mut data = Vec::with_capacity(width * height);
    for row in buf.chunks_exact(width * 2) {
        data.extend(row.chunks_exact(2).map(|px| px[0]));
    }
    GrayFrame::new(width, height, data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yuyv_takes_even_bytes() {
        // yuyv422: [Y0, U, Y1, V] × 2 像素一组；2x2 帧
        let buf = vec![
            10, 100, 20, 101, 30, 102, 40, 103, // 第一行：Y = 10,20,30,40
            50, 104, 60, 105, 70, 106, 80, 107, // 第二行：Y = 50,60,70,80
        ];
        let gray = yuyv_to_gray(&buf, 4, 2);
        assert_eq!(gray.width, 4);
        assert_eq!(gray.height, 2);
        assert_eq!(&gray.data[..], &[10, 20, 30, 40, 50, 60, 70, 80]);
    }
}
