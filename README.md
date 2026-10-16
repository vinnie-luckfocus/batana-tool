<p align="center">
  <img src="https://raw.githubusercontent.com/vinnie-luckfocus/batana/main/assets/logo.png" alt="batana" width="150">
</p>

# batana-tool — batana-core 素材采集与标注工具

MacBook + Type-C USB3 双目模组的挥棒素材生产工具：人站上打击区自动识别，预览画面大号视觉引导挥棒，自动分段保存；配套骨架叠加审核、手动修正、修剪与契约格式导出。完整需求见 [docs/prd.md](docs/prd.md)。

## 定位

- **服务对象**：batana-core 模型训练素材生产（M1.5：≥200 段标注挥棒视频）
- **采集硬件**：Type-C USB3 双目整模组（OV9281，2560×800 side-by-side，120fps 无压缩），MacBook 架设于打击区旁
- **自动化目标**：采集人只需挥棒；就位检测、视觉引导（倒计时数字→「挥棒！」→「录制中」→「已保存」绿闪）、挥棒分段、保存、编号全自动

## 功能（v0.1–v0.3 路线）

- F1 双目采集（UVC/左右目切分/无损存储/逐帧时间戳）
- F2 打击区 ROI 配置 · F3 就位检测 · F4 视觉引导（预览画面大号提示，零延迟） · F5 挥棒自动检测分段 · F6 采集会话管理
- F7 骨架自动标注（MediaPipe 33 点）· F8 骨架叠加审核与手动修正 · F9 片段修剪 · F10 审核标记与 session-schema 导出
- F11 环境合规主动判定：采集前一键 ~10s 环境体检（光照/频闪/清晰度/背景/水平/构图/帧率/磁盘 8 项，逐项中文整改建议）+ 采集中光照频闪持续监测
- 相机标定与 3D 重建：设置页抽帧调 batana-core ChArUco 双目标定（或模组规格名义标定冒烟）；审核页双骨架三角化为 pose3d（契约结构，单位米）并出无真值合理性报告（骨长稳定性/深度分布/低置信占比）

## 边界

不做训练与评分（batana-core）、不做云同步（batana-web）、不做 IMU 融合（P2b 后扩展）、仅 macOS。

## 技术栈

PySide6 + OpenCV + MediaPipe · 采集引导为预览画面大号视觉提示（无语音依赖，帧驱动零延迟）

## 使用说明

环境：macOS（arm64），推荐 Python 3.12（mediapipe 当前未支持 3.13+）。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"        # 核心层 + 测试
.venv/bin/pip install -e ".[pose]"       # 可选：MediaPipe 骨架（会换装 opencv-contrib-python，功能超集）
.venv/bin/pip install -e ".[ui]"         # 可选：PySide6（GUI 里程碑用，核心层不依赖）
```

```bash
.venv/bin/pytest                                      # 全部无头测试（202 项，含合成视频端到端与 UI 离屏）
.venv/bin/python -m app.main --gen-synth out.mkv      # 生成合成双目测试视频（2560x800 SBS，8px 视差）
.venv/bin/python -m samples.gen_synth out.mkv --fps 120 --cycles 3
.venv/bin/python -m app.envcheck --source samples/output/synth_swing.mkv --duration 5   # 无头环境体检（F11）
```

### GUI 使用

```bash
.venv/bin/python -m app.main                                   # 启动桌面 GUI（默认采集页，UVC 相机源）
.venv/bin/python -m app.main --source samples/output/synth_swing.mkv   # 视频文件回放演示（无相机）
```

三页结构（顶部页签切换）：**采集页**（实时预览 ~30fps 降频、左/右/双目并排切换、ROI 拖拽框选持久化到 settings.json、状态机大字横幅与遥测、预览中心视觉引导（倒计时/挥棒/录制中/已保存）、开始/暂停/手动挥棒/丢弃重拍/相机源选择）、**审核页**（素材列表筛选、逐帧回放、骨架叠加与拖动修正/撤销/重跑单帧、修剪、三段式标记、合格素材批量导出+校验报告）、**设置页**（采集模式/检测阈值/pre-post-roll/存储根目录）。

界面为 macOS 原生风格设计系统（`app/ui/theme.py`，Apple HIG）：控件交给系统样式引擎渲染、浅色/深色跟随系统外观、系统语义色（绿=就绪、蓝=进行中、红=异常/挥棒提示）、半透明圆角卡片 + 0.5px 发丝描边 + 同向软阴影、顶部工具栏式分段控件、状态 pill 与通知式提醒横幅、数值读数用等宽数字字体（SF Mono → Menlo 回退）。真毛玻璃由 `app/ui/vibrancy.py` 提供：通过 PyObjC 在主窗口（underWindowBackground 材质）与环境体检对话框（popover 材质）背后嵌入 NSVisualEffectView，安装可选依赖 `pip install -e ".[vibrancy]"` 后生效；offscreen 测试或未安装 PyObjC 时自动回退为半透明 QSS 近似渲染（截图环境下无窗口服务器，真机上呈现真实模糊透光）。

### 核心层 API（供 UI 层集成，全部可无头运行）

- `app.capture`：`FrameSource` protocol（`frames() -> Iterator[(frame_idx, ts_ns, sbs_frame)]`）；`UvcSource`（UVC 采集，参数化分辨率/fps/像素格式）；`FileSource`（视频回放）；`split_sbs()`（SBS 对半切分）；`RingBuffer(seconds, fps)`（线程安全预录环缓冲，`extract(start_idx, end_idx)` 区间提取）；`ClipWriter(fps).write_clip(frames, out_dir, meta)`（FFV1/MKV 优先、mp4v 回退告警，同时写 `timestamps.csv` + `capture_meta.json`）
- `app.detect`：`PresenceDetector(roi, fps, ...)`（MOG2 前景占比 + 稳定 N 帧判定就位/离场，就位后冻结背景模型）；`SwingDetector(roi, fps, pix_thresh, trigger/release_ratio, pre/post_roll)`（显著运动像素占比，底噪免疫，返回 `started`/`ended`）；`CaptureStateMachine(presence, swing, buffer, fps, clip_saver=, countdown_seconds=, max_swing_seconds=)`（PRD F5 状态机 IDLE→READY→ARMED→SWING→SAVING→READY；SAVING 挂起等 `notify_saved()` 落盘确认后才回 READY；挥棒超 max_swing 强制收尾防缓冲溢出；`feed_frame()` 逐帧驱动；`add_listener()` 迁移回调供 UI 接信号；`manual_start/stop()` 手动兜底；`discard()` 丢弃重拍；`pause()/error()/recover()`）
- `app.pose`：`PoseFrame`/`Keypoint`（33 点契约拓扑，`correct()` 手动修正保留自动原值）；`write_pose2d()/read_pose2d()`；`PoseEstimator` protocol + `StubPoseEstimator`（确定性）+ `MediaPipePoseEstimator`（Tasks PoseLandmarker，需下载 `.task` 模型文件）
- `app.session`：`SessionStore(root)`（index.json 原子落盘、三段式标记 合格/不合格/待复核、修剪、删除、`rebuild()` 崩溃重建、`counts()`）；`export_session(clip, out_root, pose_frames=, ...)`（产出契约目录）；`validate_session_builtin()`（内置必填校验）；`validate_with_core()`（本机存在 batana-core 时调其 `tools/validate_session.py` 全量校验）
- `app.envcheck`（F11）：8 项纯函数检查（光照/频闪含 FFT 市电特征识别/清晰度/背景干扰/水平/构图/帧率掉帧/磁盘，阈值全参数化见 `defaults.py`）；`EnvironmentChecker(frame_source, roi, settings, report_root)`（`run(duration_s, progress_cb)` 采样并逐项检查，复用 FrameSource 抽象，报告 JSON 落盘 `env_reports/`）；无头 CLI `python -m app.envcheck --source <视频> --duration 10`
- `app.calib`：`nominal_calibration(width, height)`（模组规格名义标定，基线 120mm，仅冒烟）；`save_calibration()/load_calibration()/calibration_status()`（~/.batana-tool/calibration.json 存档与状态摘要）；`extract_frame_pairs(clip_dir, out_dir)`（均匀抽帧为 left/right 分图）；`run_core_calibration(frames_dir, core_repo, out_json)`（subprocess 调 core ChArUco 双目标定）
- `app.recon`：`reconstruct_session(clip_dir, calib, core_repo)`（双骨架归一化点 → 像素 → core 批量三角化 → 契约 pose3d，mm→m，写 pose3d.json）；`sanity_report(pose3d)`（骨长帧间变异系数/深度分布/低置信占比 → verdict 与中文报告行）

### 本机实测结论（2026-09，macOS arm64 + OpenCV 5.0）

- **FFV1/MKV 可用**：写 30 帧随机噪声读回逐位一致（无损）；mp4v 回退路径有测试覆盖
- **mediapipe 1.0.1 可用**（Python 3.12）：Tasks `PoseLandmarker` 导入正常；旧 `mp.solutions` API 已移除，实现走 Tasks VIDEO 模式；模型文件需另行下载（`pose_landmarker_lite.task`，见 `app/pose/estimator.py`  docstring 内地址），缺失时构造抛 `FileNotFoundError` 优雅降级
- **已知问题（2026-09-20 实测）**：mediapipe 1.0.1 的 PoseLandmarker 在 **headless shell 环境（无 WindowServer 连接）**下创建即 Abort（`DrishtiMetalHelper: Service is unavailable`，CPU delegate 与 `MEDIAPIPE_DISABLE_GPU=1` 均无效，QApplication offscreen 亦无效）——macOS 构建的检测计算器无条件初始化 Metal 上下文所致。构造器已加 `delegate="cpu"` 默认参数（规避 GPU 路径的其他不确定性）。**待验证**：打包为 .app bundle（有 Info.plist/GUI 会话）后重试；备选降级路径为 mediapipe 0.10.x `mp.solutions.pose`。当前骨架链路以 Stub 估计器验证，真实推理 smoke 待上述任一环境通过
- 依赖注意：`pip install mediapipe` 会把 `opencv-python` 换装为 `opencv-contrib-python`（功能超集，FFV1 不受影响）
- **已知问题（2026-10-14，挂起待修）**：3D 骨架查看器在实机 app 中黑屏。已修并排除的原因：Qt6 中 QOpenGLShader 误从 QtGui 导入被静默吞（v0.6.1 已改 QtOpenGL 导入 + 异常打印留痕）。修复后用探针脚本（QApplication + Skeleton3DDialog 载入素材 005 + grabFramebuffer）验证**出图正常**（骨架与地面网格可见），但用户在 app 内打开仍黑屏——差异点在真实 app 运行路径。待查假设：①实机跑的 app 未更新到 v0.6.1（先确认版本）；②主窗口 NSVisualEffectView 真毛玻璃与 QOpenGLWidget 图层叠加冲突（vibrancy 仅主窗口，对话框未挂，但需验证）；③日志中 `Could not create NSOpenGLContext with shared context, falling back to unshared context` 提示共享上下文创建失败，Qt 全局默认 QSurfaceFormat 与控件 setFormat 不一致时可能拿到非 core 上下文；④多显示器/独显切换。排查入口：实机从终端启动 app（`python -m app.main`）复现黑屏，看 stderr 是否有 initializeGL traceback（v0.6.1 起有留痕），再逐条排除

### 目录结构

```
app/
├── main.py            # 入口：QApplication + 三页主窗口；--source 文件回放演示；--gen-synth 合成视频
├── capture/           # 帧源抽象/UVC/FileSource、SBS 切分、RingBuffer、ClipWriter
├── detect/            # PresenceDetector、SwingDetector、CaptureStateMachine
├── pose/              # PoseFrame 数据模型、pose2d 读写、Stub/MediaPipe 估计器
├── voice/             # Voice 抽象、SayVoice、NullVoice、中文文案
├── session/           # SessionStore 素材索引、export_session 契约导出与校验
├── calib/             # 相机标定：名义标定、标定存档、抽帧、core ChArUco 标定调用
├── recon/             # 3D 重建：双骨架三角化（调 core --batch）+ 无真值合理性报告
├── envcheck/          # F11 环境合规检查（8 项纯函数 + EnvironmentChecker + 无头 CLI）
└── ui/                # 采集页/审核页/设置页 + 设计系统（theme）+ 采集控制器（线程/信号桥）+ EnvCheckDialog
tests/                 # 202 项无头测试：核心层 + 环境检查 + 标定/重建 + 3D 查看器 + 挥棒段工作流 + UI 离屏
samples/gen_synth.py   # 合成双目视频生成器（无人→走入就位→挥棒→静止，循环）
```

## 对外契约

- 消费：**session-schema**（导出格式对齐，owner: batana-core）、**capabilities**（pose2d 拓扑约定）
- 产出：训练素材目录（session.json + 双目视频 + 时间戳 + pose2d 标注）

## 变更记录

| 版本 | 日期 | 变更内容 | 同步 |
| --- | --- | --- | --- |
| 0.7.0 | 2026-10-16 | 挥棒段工作流（M-A/M-B/M-C）：①M-A app/detect/swing_window.py——挥棒段建议：触发帧锚定基础窗口 [trig−150ms, trig+400ms]，双腕双肘可见性最高者像素速度 + 5 帧中值平滑做静默边界精修（向前找起点/向后找峰后回落，失败回退基础窗口），无触发帧退化速度峰值法（峰前 350ms/峰后 150ms），无数据整段；审核页修剪区「挥棒段建议」按钮写 trim（与手动同一更新路径，播放头跳起点）；②M-B 跑骨架加范围选择（整段/仅 trim 段，非整段 trim 时默认仅 trim 段），_PoseWorker 支持帧范围，结果按 frame_index 合并（范围外与另一目已有骨架保留），稀疏列表显示/修正/撤销按帧号定位；传播修正在 trim 边界停止；③M-C app/session/training_export.py——训练包导出：trim 段双目 PNG（6 位帧号）+ annotations.json（像素坐标、manual 标记保留、无骨架目写 null）+ meta.json；审核页「导出训练包」按钮（无骨架禁用并提示）；真实素材验证：4 段窗口 [87,167]/[84,168]/[89,168]/[92,170]（trigger=120，速度精修），素材 005 导出 85 帧×2 PNG 结构校验通过；新增 13 项测试（合计 202 项全绿 + 1 GL skip） | 待同步司令塔 repos.yaml |
| 0.6.1 | 2026-10-14 | 3D 查看器黑屏修复：根因是 Qt6 中 QOpenGLShader/QOpenGLShaderProgram 已移至 PySide6.QtOpenGL 模块，initializeGL 从 QtGui 导入抛 ImportError 且被静默吞掉 → GL 初始化失败只清背景。修复：改从 QtOpenGL 导入；initializeGL 异常改为打印 traceback 留痕（黑屏不再无线索）。真机 GL 渲染验证通过（素材 005：grabFramebuffer 实测非背景像素 >0，骨架+地面网格出图正常，189 项测试全绿） | 待同步司令塔 repos.yaml |
| 0.6.0 | 2026-10-14 | 3D 骨架查看器（审核页「查看 3D」）：①app/ui/skeleton3d.py——QOpenGLWidget 手写渲染（3.3 core + 着色器，无第三方 GL 依赖；PySide6 不导出 GL_* 枚举，常量按 GL 规范值内置），33 关节点 + BlazePose 补全连线表（身体侧配色沿用 2D 约定：左橙/右蓝/中线灰，语义色跟随外观）、y=0 地面 0.5m 网格、左目系 y 翻转为显示系 y 朝上（地面高由脚部关键点估计）、拖动旋转/右键平移/滚轮缩放、内置播放（0.25×/0.5×/1× 与审核页走带同档）+ 帧号；②app/ui/skeleton3d_math.py——相机数学与数据转换纯 numpy 无 Qt（OrbitCamera orbit/zoom/pan/lookAt/perspective/project/fit、掩蔽点转 NaN、坐标翻转），可无头单测；③审核页集成——pose3d.json 存在即可开查看器（不必重跑重建）、重建报告对话框加「查看 3D」、走带帧位置单向同步到 3D 视图（外部跟随暂停内置播放，内置播放不回写避免回路）、切素材跟随重载/关闭、删除/shutdown 关闭；GL 资源随上下文 aboutToBeDestroyed 显式释放（防退出崩溃）；修复真机冒烟发现的 PySide6 GL 常量缺失与 Qt 父子树双删警告；新增 12 项测试（相机数学 6 + 数据转换 4 + UI smoke/播放/入口同步 3，GL offscreen 不可用自动 skip；合计 189 项全绿 + 1 skip） | 待同步司令塔 repos.yaml |
| 0.5.1 | 2026-10-14 | 实机故障修复：①双目跑骨架右目初始骨架偏移——根因是 _PoseWorker 两目共用同一 MediaPipe 估计器实例（VIDEO 模式时序跟踪状态跨目续跑，右目开头从左目末帧位置偏入），改为估计器工厂每目独立实例；②直接 MediaPipe 骨架 3D 重建判「存在异常」无法通过——根因是 MediaPipe 逐帧 2D 抖动 + 双目对应不一致被裸三角化放大（实测 640×400 下 0.26–0.43px 视差抖动 ≈ 每端点 ±50mm 深度噪声）。修复（core 侧 preprocess + 批量模式扩展）：三角化前 5 帧时序中值平滑 + visibility<0.5 掩蔽（pose3d 缺失点写 null/visibility=0，契约约定）+ 极线校正（correctMatches）；sanity_report 改「链路判定 + 精度等级 A/B/C」两维口径——链路判定查系统性错误（深度/Z>0/骨长中位人体区间 + 单段超区间 2 倍即异常），精度等级用鲁棒 CV（MAD÷中位），C 级附升级建议（1280×720 档位 + 实测标定）；真实素材复跑 4 段全部「链路合理」（精度 B×1/C×3，640×400 旧档位），合成端到端仍精度 A；新增 6 项测试（合计 177 项全绿）；core 51 项全绿 | 待同步司令塔 repos.yaml |
| 0.5.0 | 2026-10-13 | 相机标定 + 3D 重建（验证 core 标定/三角化管线）：①app/calib——名义标定（HBVCAM-W2237-2 规格：基线 120mm、f_px≈1200@1280 宽线性缩放、畸变全 0，report.source=nominal-spec 标注估算）、标定存档 ~/.batana-tool/calibration.json（状态摘要区分名义/实测）、片段均匀抽帧、subprocess 调 core ChArUco 双目标定；②app/recon——双骨架（pose2d.json+pose2d_right.json）归一化点还原像素 → core 三角化批量模式 → 契约 pose3d（左目系、单位米、33 点带 name），sanity_report 无真值判据（12 段骨长帧间变异系数/深度 p5/p50/p95/Z>0 比例/低置信占比 → 「链路合理」或异常项）；③设置页「相机标定」区块（状态行 + 抽帧标定/名义标定/清除，后台 QThread 不卡 UI，core 仓不可用禁用）、审核页「3D 重建」按钮（双骨架齐全才可用，报告对话框只读可复制）、导出链路自动注入已存标定；④合成装置端到端实测：标定恢复基线 99.89mm（真值 100mm）、立体 RMS 0.093px，重建误差中位 <0.01mm；新增 14 项测试（合计 171 项全绿）；core 侧 triangulate 新增 --batch 批量模式（40 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.11 | 2026-10-13 | 双目联动（默认开，审核页可关）：①拖动一目关键点后另一目同帧同点施加相同位移（双目是同一动作两视角，错位通常双目共振），仅覆盖另一目未人工修正的点（人工结果优先），拖动+联动并入同一撤销批；②传播时联动目按自身偏移量一并传播（一次拖动+一次传播修好双目），撤销双目同批回退；新增 2 项联动测试（合计 157 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.10 | 2026-10-13 | 传播修正三次实机修复（素材#004）：旧传播结果落盘为 manual 后，重新载入素材/重启 app 时内存传播集合已清空，二次传播被自己的旧传播帧挡死（0 帧）。修复：①传播改为覆盖路径上所有帧（含旧人工/传播修正），唯一停止条件是检测恢复或片段边界，移除内存传播集合机制；②新增偏移量线性衰减——先扫描定位恢复帧，路径 k 帧第 j 帧权重 (k-j+1)/k，缓解常量偏移在长路径上的累积偏离（无恢复帧时保持常量）；撤销仍整批回退到原始自动值；重写 2 项旧语义测试 + 新增保存→重新载入→再传播全链路回归测试（合计 155 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.9 | 2026-10-12 | ①换素材不換骨架修复：双目骨架内存缓存按素材隔离（load_record 清空，此前沿用上一段素材骨架）；②默认采集档位升级 2560x720@120（采集/预览均最高分辨率，用户决策不要低分辨率）：协商失败自动回退 2560x720@60（模组该分辨率实测上限）并回写设置，不再退低分辨率；存量 1280x400 设置自动迁移 + ROI 按单目分辨率（640x400→1280x720）比例缩放；设置页手动改分辨率同样缩放 ROI；新增 2 项回归测试（合计 154 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.8 | 2026-10-12 | 双目审核修复与升级：①换目串骨架修复——根因是跑完骨架后 `_pose_dirty=True` 使切目重载永不执行 + `_load_pose_for_eye` 清空双目缓存；内存字典分目存放，切目/双目不丢未保存修改；②新增「双目并排」查看目——左右目画面同屏、骨架各归各目、各自可拖动修正（锁目不串目），悬停标签带目别前缀；③修正/传播/撤销全链路带目别（undo 批/传播集合按 (帧,点,目)）；④附带修复 MediaPipe VIDEO 模式双目推理时间戳重启违例（估计器内单调钳制，pose2d 契约时间戳保持片内真实值）；新增 3 项测试（合计 152 项全绿），实机双目渲染目检通过 | 待同步司令塔 repos.yaml |
| 0.4.7 | 2026-10-12 | 传播修正二次实机修复：机器传播的帧被标 manual 后成为"人工帧"，二次传播被自己的传播结果挡住（传播→人工微调→再传播 = 0 帧）。修复：内存态传播集合区分机器/人工帧——机器传播帧可再传播（基于原始自动值+新偏移量覆盖），人工拖动后出集合转为真正边界；恢复判定一律基于原始自动值；撤销/重跑骨架/切换素材时同步清理传播集合（150 项测试全绿） | 待同步司令塔 repos.yaml |
| 0.4.6 | 2026-10-12 | 传播修正实机修复：根因是 MediaPipe 对错位点也报高置信（实测 vis 0.84–0.93），原「vis≥0.5 即停」导致传播 0 帧。停止条件改为「高置信且脱离错位簇」（错位/卡住的点位置几乎不动，恢复时才跳离）；传播后视图自动跳到末帧便于检查边界继续修正；新增高置信卡住点回归测试（合计 149 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.5 | 2026-10-12 | 骨架标注效率三优化（实机标注反馈）：①关节识别——连线按身体侧着色（左橙/右蓝/中线灰），悬停高亮+中文关节名标签，拖动标签中文化；②连续错位一键修正——「传播到前/后帧」把修正偏移量逐帧扩散，自动检测恢复可信（visibility≥0.5）或遇人工帧即停，撤销改为按批回退（传播一批整批撤销）；③简化手部（默认开）——隐藏小指/食指/拇指 6 个细节点的绘制与命中，仅手腕可编辑，数据仍保留 33 点全量（core 契约不变）；新增 3 项测试（合计 148 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.4 | 2026-10-12 | 跑骨架修复：根因是 mediapipe 未安装（pose extra 未装）且模型路径未配置时静默回退 Stub 假骨架。mediapipe 1.1.0 入 .venv；模型工件归属 batana-core（models/files/pose_landmarker_full.task + registry.yaml 登记 uri/sha256，权重不入库）；tool 留空设置时自动探测 core 仓模型，Stub 兜底改为显式弹窗+界面标注引擎名（Stub 假骨架/MediaPipe）；实机素材验证：置信度 0.74，24/33 点可见，骨架正确叠加击球员（145 项测试全绿） | 待同步司令塔 repos.yaml |
| 0.4.3 | 2026-10-08 | 实机三连修复：①审核页切页自动刷新素材列表（此前新保存段须重启可见）；②挥棒最长时限 max_swing_seconds（默认 3s）+ 缓冲容量运行时兜底（≥ pre-roll+最长挥棒+1s）——实机取证：旧设置 buffer 3s 遇长 SWING（挥后持续走动）环形缓冲溢出，产出 trigger_idx 为负（-56/-148/-437）、挥棒起点整段丢失的残缺素材；③节奏控制：SAVING 挂起等落盘确认（notify_saved 按序号防过期），保存完成沉淀 3s（post_save_settle_seconds 可调）再倒计时，SAVING 期间预览显示「保存中…」，「已保存」不再插入倒计时中间；新增 3 项状态机测试（合计 144 项全绿） | 待同步司令塔 repos.yaml |
| 0.1-draft | 2026-09-20 | 仓库创建，PRD v1.0 定稿（docs/prd.md） | 待同步司令塔 repos.yaml |
| 0.1 | 2026-09-20 | 核心层实现：采集/检测/语音/姿态/会话导出 + 59 项无头测试 + 合成素材生成器（UI 页面待下一里程碑） | 待同步司令塔 repos.yaml |
| 0.4 | 2026-10-08 | 语音引导全面改为预览画面大号视觉提示（倒计时数字/绿框「挥棒！」/红框「录制中」/绿闪「已保存」/告警大字）：实机取证证实 say 语音队列延迟导致挥棒落在片段外；同步移植 Tauri 期修复——挥棒触发持续帧确认（25ms）、ARMED 0.3s 宽限期、预录缓冲默认 3→5s；删除 app.voice 与静音/语速设置项，142 项测试全绿 | 待同步司令塔 repos.yaml |
| 0.2 | 2026-09-20 | 桌面 UI：采集/审核/设置三页（战术遥测设计系统）、采集控制器线程信号桥、骨架叠加修正/修剪/标记/批量导出、UI 离屏冒烟 7 项（合计 66 项全绿） | 待同步司令塔 repos.yaml |
| 0.3 | 2026-09-20 | F11 环境合规主动判定（PRD v1.1）：app/envcheck 8 项检查 + 体检执行器 + 无头 CLI，采集页 [ ENV CHECK ] 对话框、采集中光照/频闪持续监测横幅、设置页环境检查阈值区，新增 41 项无头测试（合计 107 项全绿） | 待同步司令塔 repos.yaml |
| 0.3.1 | 2026-09-20 | UX 评审整改（docs/reviews/2026-09-20-ux-review.md，H1–H5/M1–M5/L1/L3–L5）：状态横幅中文化分色+视觉倒计时、审核标记后自动跳下一条待复核+跳触发帧+Shift 步进/Ctrl+Z、未保存确认、删除素材、启动 rebuild 恢复提示、异常区分相机/存储、落盘独立 worker 线程、骨架左/右/双目选择、ARMED 15s 未检测挥棒语音提示+能量阈值刻度、导出磁盘预检+后台线程进度+core 仓路径可配、ROI 引导回显、MJPEG 警告、采集快捷键（空格/D）、回放 0.25×/0.5× 慢放、重复导出注明覆盖、设置页磁盘占用估算；新增 18 项测试（合计 125 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.0 | 2026-09-20 | UI 全面改版为 macOS 原生风格（Apple HIG）：原生样式引擎 + 跟随系统浅色/深色、系统语义色状态 pill（绿=就绪/蓝=进行中/红=异常）、顶部工具栏式分段控件、圆角卡片与发丝分隔、审核页空状态引导视图、环境提醒改通知横幅式、ROI 系统红半透明填充+四角手柄、骨架置信度柔和系统色、移除全部 ASCII 装饰与全大写标题；功能行为零变化（125 项测试全绿） | 待同步司令塔 repos.yaml |
| 0.4.1 | 2026-09-20 | Liquid Glass 深度打磨：PyObjC NSVisualEffectView 真毛玻璃（主窗口 underWindowBackground / 体检对话框 popover，新增 vibrancy 可选依赖，offscreen/未安装时优雅回退半透明 QSS）、卡片半透明面+发丝描边+同向着色软阴影、prominent 蓝主按钮、分段控件选中浮起、访达式圆角半透明选中行、状态 pill 内描边高光、空态引导视图加图标位+行动按钮、体检对话框标题层级（22pt 标题 + 13pt 次级说明）；新增 vibrancy 回退测试（合计 126 项全绿） | 待同步司令塔 repos.yaml |
| 0.4.2 | 2026-09-20 | 对齐/主题切换/自适应三向精修：审核页底部三栏改等宽网格（列标题与首行控件基线对齐）、走带控件等高成一线、采集页遥测两列等宽网格+能量条拉满、设置页表单标签列等宽右对齐、卡片内边距统一 16px；浅/深色运行时切换（监听 colorSchemeChanged 重挂 QSS，StateBanner/通知横幅/相机指示/MiniBar/ROI/骨架叠加经 paletteChange 重取语义色，修重入递归）；主窗口最小尺寸 1100×680、素材列表弹性 240–320px（窄栏省略号不截断）、走带进度条最小宽度；新增主题刷新与最小尺寸 smoke 测试（合计 128 项全绿） | 待同步司令塔 repos.yaml |

## 许可证

见 [LICENSE](LICENSE)。
