//! 会话层：素材目录登记、session-schema v1.0 导出、设置持久化。

pub mod export;
pub mod pose;
pub mod settings;
pub mod store;
pub mod ulid;

use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use crate::capture::AppState;

pub use settings::AppSettings;
pub use store::{SessionStore, STATUS_FAIL, STATUS_PASS, STATUS_REVIEW};
pub use ulid::new_session_id;

/// 素材列表条目（index.json 记录的摘要投影）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionSummary {
    pub id: String,
    pub created_at: String,
    pub date: String,
    pub seq: u64,
    /// 三段式审核标记：合格 / 不合格 / 待复核
    pub verdict: String,
    pub frame_count: u64,
    pub fps: f64,
    pub clip_dir: String,
}

impl SessionSummary {
    fn from_record(r: &serde_json::Value) -> Self {
        Self {
            id: r["session_id"].as_str().unwrap_or_default().into(),
            created_at: r["created_at"].as_str().unwrap_or_default().into(),
            date: r["date"].as_str().unwrap_or_default().into(),
            seq: r["seq"].as_u64().unwrap_or(0),
            verdict: r["status"].as_str().unwrap_or(STATUS_REVIEW).into(),
            frame_count: r["frame_count"].as_u64().unwrap_or(0),
            fps: r["fps"].as_f64().unwrap_or(0.0),
            clip_dir: r["clip_dir"].as_str().unwrap_or_default().into(),
        }
    }
}

/// 素材列表：新段在前。素材库打不开（如目录不可写）返回空列表而非报错，
/// 与 Python 版 SessionStore 构造即建目录的容错语义对齐。
#[tauri::command]
pub fn list_sessions(state: tauri::State<'_, Arc<Mutex<AppState>>>) -> Vec<SessionSummary> {
    let st = match state.lock() {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let Ok(store) = SessionStore::new(&st.settings.storage_root) else {
        return Vec::new();
    };
    let mut items: Vec<SessionSummary> = store
        .list(None)
        .iter()
        .map(SessionSummary::from_record)
        .collect();
    items.reverse(); // 新段在前
    items
}

#[tauri::command]
pub fn get_settings(state: tauri::State<'_, Arc<Mutex<AppState>>>) -> AppSettings {
    state.lock().map(|s| s.settings.clone()).unwrap_or_default()
}

/// 保存设置并更新内存态；正在运行的采集保持启动时的快照，下次启动生效。
#[tauri::command]
pub fn save_settings(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    settings: AppSettings,
) -> Result<(), String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let path = st.settings_path.clone();
    settings.save(&path)?;
    st.settings = settings;
    Ok(())
}

/// 三段式审核标记（合格 / 不合格 / 待复核），立即落盘 index.json。
#[tauri::command]
pub fn mark_session(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    session_id: String,
    status: String,
) -> Result<(), String> {
    let root = state.lock().map_err(|e| e.to_string())?.settings.storage_root.clone();
    let mut store = SessionStore::new(&root)?;
    store.mark(&session_id, &status)
}

/// 删除素材：移除索引记录并连同素材目录一并删除（重拍场景）。
#[tauri::command]
pub fn delete_session(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    session_id: String,
) -> Result<(), String> {
    let root = state.lock().map_err(|e| e.to_string())?.settings.storage_root.clone();
    let mut store = SessionStore::new(&root)?;
    store.delete(&session_id, true)
}

/// 读取素材骨架数据 pose2d.json；不存在返回 None。
#[tauri::command]
pub fn read_pose2d(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    session_id: String,
) -> Option<serde_json::Value> {
    let root = state.lock().ok()?.settings.storage_root.clone();
    let path = Path::new(&root).join("sessions").join(&session_id).join("pose2d.json");
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

/// 复核页预览转码：FFV1/MKV 无损母版 WebView 无法直接解码，
/// 按需用 ffmpeg 转出 H.264 预览 mp4（缓存在素材目录内 preview_<eye>.mp4）。
/// 返回预览文件绝对路径（前端经 asset 协议加载）。
#[tauri::command]
pub async fn ensure_preview_video(
    state: tauri::State<'_, Arc<Mutex<AppState>>>,
    session_id: String,
    eye: String,
) -> Result<String, String> {
    if eye != "left" && eye != "right" {
        return Err(format!("非法目: {eye}"));
    }
    let root = state.lock().map_err(|e| e.to_string())?.settings.storage_root.clone();
    let dir = Path::new(&root).join("sessions").join(&session_id);
    let src = dir.join(format!("{eye}.mkv"));
    let dst = dir.join(format!("preview_{eye}.mp4"));
    if dst.is_file() {
        return Ok(dst.to_string_lossy().into_owned());
    }
    if !src.is_file() {
        return Err(format!("视频不存在: {}", src.display()));
    }
    let ffmpeg = crate::capture::find_in_path("ffmpeg").ok_or("未找到 ffmpeg（brew install ffmpeg）")?;
    let out = std::process::Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-y", "-i"])
        .arg(&src)
        .args(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"])
        .arg(&dst)
        .output()
        .map_err(|e| format!("启动 ffmpeg 转码失败: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "预览转码失败: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(dst.to_string_lossy().into_owned())
}
