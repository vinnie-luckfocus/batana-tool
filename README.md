<!-- PROJECT LOGO -->
<br />
<div align="center">
  <a href="https://github.com/vinnie-luckfocus/batana">
    <img src="docs/assets/logo.png" alt="Batana Logo" width="200" height="200">
  </a>

  <h3 align="center">BatanaTool</h3>

  <p align="center">
    Batana 生态 · 挥棒素材采集与标注工具（macOS 桌面）
    <br />
    双目采集 · 自动就位/挥棒检测 · 语音引导 · 骨架标注 · 契约导出
    <br />
    <a href="https://github.com/vinnie-luckfocus/batana-tool/releases">下载</a>
    ·
    <a href="https://github.com/vinnie-luckfocus/batana-tool/issues">报告问题</a>
    ·
    <a href="https://github.com/vinnie-luckfocus/batana">Batana 司令塔</a>
  </p>
</div>

## 简介

BatanaTool 是为 [batana-core](https://github.com/vinnie-luckfocus/batana-core) 模型训练高效生产标注素材的 macOS 桌面应用：MacBook + Type-C USB3 双目模组架设于打击区旁，人站上打击区自动识别就位，中文语音引导挥棒，挥棒自动检测分段保存；配套骨架叠加复核、手动修正、修剪与契约格式导出。

产出的素材严格遵循 batana-core 的 **session-schema v1.0** 契约，导出即过 `validate_session.py` 全量校验（core↔tool 硬同步）。

## 功能

- **双目采集**：UVC 免驱接入、设备自动识别、左右目切分、FFV1 无损存储、逐帧时间戳
- **自动编排**：打击区 ROI 框选、就位检测（静止不误判离场）、中文语音引导（`say`）、挥棒运动占比检测 + 预录环缓冲自动分段
- **素材复核**：列表筛选、双目回放、逐帧步进、骨架叠加、合格/不合格/待复核三段式标记
- **设置**：采集档位、检测阈值、语音、存储路径，即存即用
- **macOS 原生体验**：NSVisualEffectView 毛玻璃、跟随系统深浅色、Liquid Glass 观感（遵循 [Batana 生态 UI/UX 规范](https://github.com/vinnie-luckfocus/batana/blob/main/docs/uiux-guidelines.md)）

## 技术栈

**Tauri 2（Rust + 原生 TypeScript WebView）**，采集与编码经 ffmpeg 子进程：

```
src/                    # 前端：三页 UI + 设计令牌（无框架）
src-tauri/src/
├── capture/            # ffmpeg avfoundation 帧源、设备枚举、SBS 切分、环缓冲、落盘
├── detect/             # 就位检测（就位冻结背景）+ 挥棒检测（运动像素占比判据）
├── state.rs            # 状态机 IDLE→READY→ARMED→SWING→SAVING
├── session/            # session-schema 导出、素材库索引、设置持久化
└── voice.rs            # macOS say 语音引导
```

> PySide6 旧实现留存于 tag `archive/pyside6` 仅作参考。

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

目标采集模组：HBVCAM-W2237-2（OV9281 双目全局快门，基线 120mm，USB3/Type-C，UVC 免驱）。macOS 实测档位与验证报告见 [batana-pi 调研文档](https://github.com/vinnie-luckfocus/batana-pi/blob/main/docs/research/2026-09-21-camera-macos-driver-verification.md)。

## 相关仓库

- [batana](https://github.com/vinnie-luckfocus/batana) — 生态司令塔（架构 / 路线图 / UI/UX 规范 / 版本矩阵）
- [batana-core](https://github.com/vinnie-luckfocus/batana-core) — 模型系统核心（本工具产出的素材由其消费）

## 许可证

本项目基于 MIT 许可证开源 - 查看 [LICENSE](LICENSE) 了解详情。
