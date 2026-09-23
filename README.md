# BatanaTool

Batana 生态的**素材采集与标注工具**（macOS 桌面）：双目相机采集 → 就位/挥棒自动检测 → 语音引导 → 素材落盘 → 骨架标注与复核。产出的素材严格遵循 batana-core 的 session-schema 契约（core↔tool 硬同步，见司令塔 `docs/versioning.md` §4）。

> 2026-09-23 起基于 **Tauri 2（Rust + WebView）** 重构；PySide6 旧实现留存于 tag `archive/pyside6` 仅作参考。

## 架构

```
src/                    # 前端（Vite + 原生 TypeScript，无框架）
├── ui/                 #   三页：采集 / 素材复核 / 设置
└── styles/             #   设计令牌（tokens.css，映射司令塔 UI/UX 规范）
src-tauri/              # Rust 后端
└── src/
    ├── capture/        # ffmpeg avfoundation 帧源（yuyv422 管道）、设备枚举、
    │                   # SBS 切分、环形缓冲、片段落盘（FFV1/MKV）
    ├── detect/         # 就位检测（背景建模，就位冻结）+ 挥棒检测（运动像素占比）
    ├── state.rs        # 状态机 IDLE→READY→ARMED→SWING→SAVING
    ├── session/        # session-schema v1.0 导出、素材库索引、设置持久化
    └── voice.rs        # macOS say 语音引导
```

UI/UX 遵循司令塔统一规范：`batana/docs/uiux-guidelines.md`（设计令牌禁止偏离）。

## 开发

```bash
# 前置：Rust（cargo）、Node.js、ffmpeg（brew install ffmpeg）
npm install
npm run tauri dev        # 开发模式（vite 热更新 + 毛玻璃窗口）

# 测试（64 项，含 batana-core 契约校验集成测试）
cd src-tauri && TAURI_MACOS_PRIVATE_API=1 cargo test

# 打包 .app / dmg
npm run tauri build
```

## 硬件

目标采集模组：HBVCAM-W2237-2（OV9281 双目全局快门，USB3/Type-C，UVC 免驱）。macOS 侧经 ffmpeg avfoundation 采集，实测档位见 batana-pi `docs/research/2026-09-21-camera-macos-driver-verification.md`。

## 许可证

MIT
