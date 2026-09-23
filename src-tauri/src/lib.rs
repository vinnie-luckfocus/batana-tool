//! BatanaTool 后端：UVC 双目采集（ffmpeg）→ 就位/挥棒检测 → 素材落盘 → session 导出。
//!
//! 架构：采集线程读 ffmpeg rawvideo 管道帧 → SBS 切分 → 状态机（纯 Rust 实现，
//! 语义与 Python 版一致，见 tag archive/pyside6）→ 片段经 ffmpeg 编码 FFV1 落盘。

pub mod capture;
pub mod detect;
pub mod session;
pub mod state;
pub mod voice;

use std::sync::{Arc, Mutex};

use tauri::Manager;

#[cfg(target_os = "macos")]
use window_vibrancy::{apply_vibrancy, NSVisualEffectMaterial};

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let window = app.get_webview_window("main").unwrap();
            // macOS 原生毛玻璃：侧栏材质（规范第 2 节：材质分层，窗口背景透出桌面模糊）
            #[cfg(target_os = "macos")]
            apply_vibrancy(&window, NSVisualEffectMaterial::Sidebar, None, None)
                .expect("vibrancy 应用失败");
            // 全局应用状态：设置 + 采集句柄
            app.manage(Arc::new(Mutex::new(capture::AppState::load())));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            capture::list_devices,
            capture::start_capture,
            capture::stop_capture,
            capture::discard_clip,
            capture::manual_toggle,
            capture::set_muted,
            session::list_sessions,
            session::get_settings,
            session::save_settings,
            session::mark_session,
            session::delete_session,
            session::read_pose2d,
            session::ensure_preview_video,
        ])
        .run(tauri::generate_context!())
        .expect("运行 BatanaTool 出错");
}
