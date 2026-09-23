// BatanaTool — Tauri 应用入口。业务模块：capture / detect / session / voice。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    batana_tool_lib::run()
}
