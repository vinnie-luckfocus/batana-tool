<p align="center">
  <img src="https://raw.githubusercontent.com/vinnie-luckfocus/batana/main/assets/logo.png" alt="batana" width="150">
</p>

# batana-tool — batana-core 素材采集与标注工具

MacBook + Type-C USB3 双目模组的挥棒素材生产工具：人站上打击区自动识别，语音引导挥棒，自动分段保存；配套骨架叠加审核、手动修正、修剪与契约格式导出。完整需求见 [docs/prd.md](docs/prd.md)。

## 定位

- **服务对象**：batana-core 模型训练素材生产（M1.5：≥200 段标注挥棒视频）
- **采集硬件**：Type-C USB3 双目整模组（OV9281，2560×800 side-by-side，120fps 无压缩），MacBook 架设于打击区旁
- **自动化目标**：采集人只需挥棒；就位检测、语音引导、挥棒分段、保存、编号全自动

## 功能（v0.1–v0.3 路线）

- F1 双目采集（UVC/左右目切分/无损存储/逐帧时间戳）
- F2 打击区 ROI 配置 · F3 就位检测 · F4 中文语音引导 · F5 挥棒自动检测分段 · F6 采集会话管理
- F7 骨架自动标注（MediaPipe 33 点）· F8 骨架叠加审核与手动修正 · F9 片段修剪 · F10 审核标记与 session-schema 导出

## 边界

不做训练与评分（batana-core）、不做云同步（batana-web）、不做 IMU 融合（P2b 后扩展）、仅 macOS。

## 技术栈

PySide6 + OpenCV + MediaPipe · 语音走 macOS `say`（中文 Tingting）

## 对外契约

- 消费：**session-schema**（导出格式对齐，owner: batana-core）、**capabilities**（pose2d 拓扑约定）
- 产出：训练素材目录（session.json + 双目视频 + 时间戳 + pose2d 标注）

## 变更记录

| 版本 | 日期 | 变更内容 | 同步 |
| --- | --- | --- | --- |
| 0.1-draft | 2026-09-20 | 仓库创建，PRD v1.0 定稿（docs/prd.md） | 待同步司令塔 repos.yaml |

## 许可证

见 [LICENSE](LICENSE)。
