//! 设置持久化：settings.json（ROI、采集模式、检测阈值、语音、存储根目录）。
//! 语义对齐 Python 版 app/ui/settings.py；默认路径 ~/.batana-tool/settings.json
//! 保持兼容（字段名 snake_case 与 Python 版一致）。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::capture::Roi;

/// 默认设置文件路径：~/.batana-tool/settings.json
pub fn default_settings_path() -> PathBuf {
    home_dir().join(".batana-tool").join("settings.json")
}

/// 默认存储根目录：~/batana-sessions
pub fn default_storage_root() -> String {
    home_dir().join("batana-sessions").to_string_lossy().into_owned()
}

pub fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

fn default_camera_name() -> String {
    "USB Global Camera".into()
}
fn default_capture_width() -> u32 {
    1280
}
fn default_capture_height() -> u32 {
    400
}
fn default_capture_fps() -> f64 {
    120.0
}
fn default_pixel_format() -> String {
    "auto".into()
}
fn default_capture_backend() -> String {
    "auto".into()
}
fn default_presence_ratio() -> f64 {
    0.02
}
fn default_motion_trigger_pct() -> f64 {
    2.0
}
fn default_motion_release_pct() -> f64 {
    0.8
}
fn default_motion_pix_thresh() -> f64 {
    25.0
}
fn default_roll() -> f64 {
    1.0
}
fn default_countdown() -> f64 {
    3.0
}
fn default_buffer_seconds() -> f64 {
    3.0
}
fn default_true() -> bool {
    true
}
fn default_voice_rate() -> u32 {
    200
}
fn default_env_brightness_fail() -> f64 {
    60.0
}
fn default_env_brightness_warn() -> f64 {
    90.0
}
fn default_env_flicker_warn_pct() -> f64 {
    2.0
}
fn default_env_flicker_fail_pct() -> f64 {
    5.0
}
fn default_env_sharpness_warn() -> f64 {
    30.0
}
fn default_env_level_warn_deg() -> f64 {
    2.0
}
fn default_env_level_fail_deg() -> f64 {
    5.0
}
fn default_env_planned_clips() -> u32 {
    200
}
fn default_env_est_mb_per_clip() -> f64 {
    500.0
}

/// 采集与界面参数（对应 PRD F2/F3/F4/F5 可调项）。
/// 容器级 serde(default)：缺字段用默认值、未知字段忽略——与 Python 版
/// 「从默认值出发覆盖已知键」的读取语义一致。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppSettings {
    // F2 打击区 ROI（左目像素坐标 [x, y, w, h]）；None 表示未框选
    pub roi: Option<[u32; 4]>,
    // F1 采集模式（HBVCAM-W2237-2 实测：SBS 1280x400@120 标称 / macOS 实测 75fps）
    pub capture_width: u32,
    pub capture_height: u32,
    pub capture_fps: f64,
    pub pixel_format: String,   // auto / mono8 / yuy2 / mjpeg
    pub camera_index: u32,
    pub camera_name: String,    // ffmpeg 后端按名称选设备，避免索引漂移
    pub capture_backend: String, // auto / ffmpeg / opencv；macOS 下 auto 优先 ffmpeg
    // F3/F5 检测阈值
    pub presence_ratio: f64,
    // 挥棒判据 = 显著运动像素占比（帧差 > pix_thresh 的像素占 ROI 比例），百分数
    pub motion_trigger_pct: f64,
    pub motion_release_pct: f64,
    pub motion_pix_thresh: f64,
    pub pre_roll_seconds: f64,
    pub post_roll_seconds: f64,
    pub countdown_seconds: f64,
    pub buffer_seconds: f64,
    // F4 语音
    pub voice_enabled: bool,
    pub voice_rate: u32, // say -r（词/分钟）
    // F6 存储
    pub storage_root: String,
    // F7 骨架（MediaPipe 模型文件路径；空 = 未配置）
    pub pose_model_path: String,
    // F10 导出校验：batana-core 仓路径（空 = 自动探测常见位置）
    pub core_repo_path: String,
    // F11 环境检查阈值
    pub env_brightness_fail: f64,
    pub env_brightness_warn: f64,
    pub env_flicker_warn_pct: f64,
    pub env_flicker_fail_pct: f64,
    pub env_sharpness_warn: f64,
    pub env_level_warn_deg: f64,
    pub env_level_fail_deg: f64,
    pub env_planned_clips: u32,
    pub env_est_mb_per_clip: f64,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            roi: None,
            capture_width: default_capture_width(),
            capture_height: default_capture_height(),
            capture_fps: default_capture_fps(),
            pixel_format: default_pixel_format(),
            camera_index: 0,
            camera_name: default_camera_name(),
            capture_backend: default_capture_backend(),
            presence_ratio: default_presence_ratio(),
            motion_trigger_pct: default_motion_trigger_pct(),
            motion_release_pct: default_motion_release_pct(),
            motion_pix_thresh: default_motion_pix_thresh(),
            pre_roll_seconds: default_roll(),
            post_roll_seconds: default_roll(),
            countdown_seconds: default_countdown(),
            buffer_seconds: default_buffer_seconds(),
            voice_enabled: default_true(),
            voice_rate: default_voice_rate(),
            storage_root: default_storage_root(),
            pose_model_path: String::new(),
            core_repo_path: String::new(),
            env_brightness_fail: default_env_brightness_fail(),
            env_brightness_warn: default_env_brightness_warn(),
            env_flicker_warn_pct: default_env_flicker_warn_pct(),
            env_flicker_fail_pct: default_env_flicker_fail_pct(),
            env_sharpness_warn: default_env_sharpness_warn(),
            env_level_warn_deg: default_env_level_warn_deg(),
            env_level_fail_deg: default_env_level_fail_deg(),
            env_planned_clips: default_env_planned_clips(),
            env_est_mb_per_clip: default_env_est_mb_per_clip(),
        }
    }
}

impl AppSettings {
    pub fn load(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    /// 原子落盘：先写临时文件再 rename，避免崩溃留下半截 JSON。
    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("创建设置目录失败: {e}"))?;
        }
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        std::fs::write(&tmp, text).map_err(|e| format!("写设置失败: {e}"))?;
        std::fs::rename(&tmp, path).map_err(|e| format!("替换设置文件失败: {e}"))?;
        Ok(())
    }

    /// ROI 便捷访问：(x, y, w, h) 像素坐标
    pub fn roi_tuple(&self) -> Option<Roi> {
        self.roi.map(|r| (r[0] as usize, r[1] as usize, r[2] as usize, r[3] as usize))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_roundtrip() {
        let dir = std::env::temp_dir().join(format!("batana-settings-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("settings.json");
        let mut s = AppSettings::default();
        s.roi = Some([10, 20, 300, 200]);
        s.motion_trigger_pct = 3.5;
        s.voice_enabled = false;
        s.save(&path).unwrap();
        let loaded = AppSettings::load(&path);
        assert_eq!(loaded.roi, Some([10, 20, 300, 200]));
        assert_eq!(loaded.motion_trigger_pct, 3.5);
        assert!(!loaded.voice_enabled);
        assert_eq!(loaded.camera_name, "USB Global Camera");
        assert_eq!(loaded.roi_tuple(), Some((10, 20, 300, 200)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn settings_partial_json_fills_defaults() {
        // Python 版 settings.json 只覆盖已知键；缺字段必须落到默认值
        let dir = std::env::temp_dir().join(format!("batana-settings-partial-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("settings.json");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(&path, r#"{"camera_name": "X", "unknown_key": 1}"#).unwrap();
        let s = AppSettings::load(&path);
        assert_eq!(s.camera_name, "X");
        assert_eq!(s.capture_width, 1280);
        assert_eq!(s.capture_fps, 120.0);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
