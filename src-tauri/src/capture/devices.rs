//! UVC 设备枚举：macOS 下解析 ffmpeg avfoundation 设备列表。
//! 语义对齐 Python 版 app/capture/devices.py。
//!
//! ffmpeg -f avfoundation -list_devices true -i "" 的输出（stderr）形如：
//!
//! ```text
//! [AVFoundation indev @ 0x...] AVFoundation video devices:
//! [AVFoundation indev @ 0x...] [0] FaceTime高清相机
//! [AVFoundation indev @ 0x...] [1] USB Global Camera
//! [AVFoundation indev @ 0x...] AVFoundation audio devices:
//! ...
//! ```
//!
//! 解析逻辑与进程调用分离，可无头测试。

use serde::{Deserialize, Serialize};

use super::find_in_path;

/// 已知的双目模组名称（HBVCAM-W2237-2 等 SunplusIT 整模组）
pub const STEREO_MODULE_NAMES: [&str; 1] = ["USB Global Camera"];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VideoDevice {
    pub index: u32,
    pub name: String,
    pub is_stereo_module: bool,
}

impl VideoDevice {
    pub fn new(index: u32, name: impl Into<String>) -> Self {
        let name = name.into();
        let is_stereo_module = STEREO_MODULE_NAMES.iter().any(|k| name.contains(k));
        Self {
            index,
            name,
            is_stereo_module,
        }
    }

    pub fn is_screen(&self) -> bool {
        self.name.starts_with("Capture screen")
    }
}

/// 解析 ffmpeg -list_devices 输出，仅取 video 段，过滤屏幕采集项。
pub fn parse_device_list(output: &str) -> Vec<VideoDevice> {
    let mut devices = Vec::new();
    let mut in_video = false;
    for line in output.lines() {
        if line.contains("AVFoundation video devices:") {
            in_video = true;
            continue;
        }
        if line.contains("AVFoundation audio devices:") {
            in_video = false;
            continue;
        }
        if in_video {
            if let Some(dev) = parse_device_line(line) {
                if !dev.is_screen() {
                    devices.push(dev);
                }
            }
        }
    }
    devices
}

/// 匹配 "] [3] 设备名" 形式的行（等价 Python 正则 r"\] \[(\d+)\] (.+?)\s*$"）。
fn parse_device_line(line: &str) -> Option<VideoDevice> {
    let tail = line.trim_end();
    let lb = tail.rfind("] [")?;
    let after = &tail[lb + 3..];
    let rb = after.find(']')?;
    let index: u32 = after[..rb].trim().parse().ok()?;
    let name = after[rb + 1..].trim();
    if name.is_empty() {
        return None;
    }
    Some(VideoDevice::new(index, name))
}

/// 枚举 avfoundation 视频设备；ffmpeg 缺失或失败时返回空列表。
pub fn list_video_devices() -> Vec<VideoDevice> {
    let Some(ffmpeg) = find_in_path("ffmpeg") else {
        return Vec::new();
    };
    let output = std::process::Command::new(ffmpeg)
        .args([
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ])
        .output();
    match output {
        Ok(out) => {
            let mut text = String::from_utf8_lossy(&out.stderr).into_owned();
            text.push('\n');
            text.push_str(&String::from_utf8_lossy(&out.stdout));
            parse_device_list(&text)
        }
        Err(_) => Vec::new(),
    }
}

/// 默认选择：优先双目模组，其次第一个非 FaceTime 外设，否则第一台。
pub fn pick_default(devices: &[VideoDevice]) -> Option<&VideoDevice> {
    if devices.is_empty() {
        return None;
    }
    for d in devices {
        if d.is_stereo_module {
            return Some(d);
        }
    }
    for d in devices {
        if !d.name.contains("FaceTime") {
            return Some(d);
        }
    }
    devices.first()
}

#[cfg(test)]
mod tests {
    use super::*;

    const FFMPEG_SAMPLE: &str = "[AVFoundation indev @ 0x7f92c14140] AVFoundation video devices:
[AVFoundation indev @ 0x7f92c14140] [0] FaceTime高清相机
[AVFoundation indev @ 0x7f92c14140] [1] USB Global Camera
[AVFoundation indev @ 0x7f92c14140] [2] “iiiv1nn1e_15”的相机
[AVFoundation indev @ 0x7f92c14140] [3] “iiiv1nn1e_15”的桌上视角相机
[AVFoundation indev @ 0x7f92c14140] [4] Capture screen 0
[AVFoundation indev @ 0x7f92c14140] AVFoundation audio devices:
[AVFoundation indev @ 0x7f92c14140] [0] MacBook Pro麦克风
[AVFoundation indev @ 0x7f92c14140] [1] “iiiv1nn1e_15”的麦克风
";

    #[test]
    fn parse_video_section_only() {
        let devices = parse_device_list(FFMPEG_SAMPLE);
        let names: Vec<&str> = devices.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "FaceTime高清相机",
                "USB Global Camera",
                "“iiiv1nn1e_15”的相机",
                "“iiiv1nn1e_15”的桌上视角相机",
            ]
        ); // 屏幕采集与音频设备被过滤
        assert_eq!(devices[1].index, 1);
        assert!(devices[1].is_stereo_module);
        assert!(!devices[0].is_stereo_module);
    }

    #[test]
    fn parse_empty() {
        assert!(parse_device_list("").is_empty());
        assert!(parse_device_list("no devices here").is_empty());
    }

    #[test]
    fn pick_default_prefers_stereo_module() {
        let devices = parse_device_list(FFMPEG_SAMPLE);
        assert_eq!(pick_default(&devices).unwrap().name, "USB Global Camera");
    }

    #[test]
    fn pick_default_skips_facetime_when_no_module() {
        let devices = vec![
            VideoDevice::new(0, "FaceTime高清相机"),
            VideoDevice::new(2, "Some USB Cam"),
        ];
        assert_eq!(pick_default(&devices).unwrap().index, 2);
    }

    #[test]
    fn pick_default_fallback_first() {
        let devices = vec![VideoDevice::new(0, "FaceTime高清相机")];
        assert_eq!(pick_default(&devices).unwrap().index, 0);
        assert!(pick_default(&[]).is_none());
    }
}
