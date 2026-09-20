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

## 使用说明

环境：macOS（arm64），推荐 Python 3.12（mediapipe 当前未支持 3.13+）。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"        # 核心层 + 测试
.venv/bin/pip install -e ".[pose]"       # 可选：MediaPipe 骨架（会换装 opencv-contrib-python，功能超集）
.venv/bin/pip install -e ".[ui]"         # 可选：PySide6（GUI 里程碑用，核心层不依赖）
```

```bash
.venv/bin/pytest                                      # 全部无头测试（59 项，含合成视频端到端）
.venv/bin/python -m app.main --gen-synth out.mkv      # 生成合成双目测试视频（2560x800 SBS，8px 视差）
.venv/bin/python -m samples.gen_synth out.mkv --fps 120 --cycles 3
```

### 核心层 API（供 UI 层集成，全部可无头运行）

- `app.capture`：`FrameSource` protocol（`frames() -> Iterator[(frame_idx, ts_ns, sbs_frame)]`）；`UvcSource`（UVC 采集，参数化分辨率/fps/像素格式）；`FileSource`（视频回放）；`split_sbs()`（SBS 对半切分）；`RingBuffer(seconds, fps)`（线程安全预录环缓冲，`extract(start_idx, end_idx)` 区间提取）；`ClipWriter(fps).write_clip(frames, out_dir, meta)`（FFV1/MKV 优先、mp4v 回退告警，同时写 `timestamps.csv` + `capture_meta.json`）
- `app.detect`：`PresenceDetector(roi, fps, ...)`（MOG2 前景占比 + 稳定 N 帧判定就位/离场，自适应学习率）；`SwingDetector(roi, fps, trigger/release_thresh, pre/post_roll)`（帧差运动能量，返回 `started`/`ended`）；`CaptureStateMachine(presence, swing, buffer, fps, voice=, clip_saver=)`（PRD F5 状态机 IDLE→READY→ARMED→SWING→SAVING→READY；`feed_frame()` 逐帧驱动；`add_listener()` 迁移回调供 UI 接信号；`manual_start/stop()` 手动兜底；`discard()` 丢弃重拍；`pause()/error()/recover()`）
- `app.voice`：`Voice` protocol（`speak(text, priority)`）；`SayVoice`（macOS `say -v Tingting` 队列串行、可静音）；`NullVoice`（测试）；中文文案常量（PRD F4）
- `app.pose`：`PoseFrame`/`Keypoint`（33 点契约拓扑，`correct()` 手动修正保留自动原值）；`write_pose2d()/read_pose2d()`；`PoseEstimator` protocol + `StubPoseEstimator`（确定性）+ `MediaPipePoseEstimator`（Tasks PoseLandmarker，需下载 `.task` 模型文件）
- `app.session`：`SessionStore(root)`（index.json 原子落盘、三段式标记 合格/不合格/待复核、修剪、删除、`rebuild()` 崩溃重建、`counts()`）；`export_session(clip, out_root, pose_frames=, ...)`（产出契约目录）；`validate_session_builtin()`（内置必填校验）；`validate_with_core()`（本机存在 batana-core 时调其 `tools/validate_session.py` 全量校验）

### 本机实测结论（2026-09，macOS arm64 + OpenCV 5.0）

- **FFV1/MKV 可用**：写 30 帧随机噪声读回逐位一致（无损）；mp4v 回退路径有测试覆盖
- **mediapipe 1.0.1 可用**（Python 3.12）：Tasks `PoseLandmarker` 导入正常；旧 `mp.solutions` API 已移除，实现走 Tasks VIDEO 模式；模型文件需另行下载（`pose_landmarker_lite.task`，见 `app/pose/estimator.py`  docstring 内地址），缺失时构造抛 `FileNotFoundError` 优雅降级
- 依赖注意：`pip install mediapipe` 会把 `opencv-python` 换装为 `opencv-contrib-python`（功能超集，FFV1 不受影响）

### 目录结构（核心层已实现，UI 页面待下一里程碑）

```
app/
├── main.py            # 入口（当前提供合成视频生成命令；GUI 待实现）
├── capture/           # 帧源抽象/UVC/FileSource、SBS 切分、RingBuffer、ClipWriter
├── detect/            # PresenceDetector、SwingDetector、CaptureStateMachine
├── pose/              # PoseFrame 数据模型、pose2d 读写、Stub/MediaPipe 估计器
├── voice/             # Voice 抽象、SayVoice、NullVoice、中文文案
└── session/           # SessionStore 素材索引、export_session 契约导出与校验
tests/                 # 59 项无头测试：状态机/检测/环缓冲/落盘/导出/pose + 端到端
samples/gen_synth.py   # 合成双目视频生成器（无人→走入就位→挥棒→静止，循环）
```

## 对外契约

- 消费：**session-schema**（导出格式对齐，owner: batana-core）、**capabilities**（pose2d 拓扑约定）
- 产出：训练素材目录（session.json + 双目视频 + 时间戳 + pose2d 标注）

## 变更记录

| 版本 | 日期 | 变更内容 | 同步 |
| --- | --- | --- | --- |
| 0.1-draft | 2026-09-20 | 仓库创建，PRD v1.0 定稿（docs/prd.md） | 待同步司令塔 repos.yaml |
| 0.1 | 2026-09-20 | 核心层实现：采集/检测/语音/姿态/会话导出 + 59 项无头测试 + 合成素材生成器（UI 页面待下一里程碑） | 待同步司令塔 repos.yaml |

## 许可证

见 [LICENSE](LICENSE)。
