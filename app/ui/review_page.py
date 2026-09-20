"""审核页（PRD F7–F10 界面）：素材列表 + 回放 + 骨架叠加/修正 + 修剪 + 标记 + 导出。"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.pose import PoseEstimator, PoseFrame, StubPoseEstimator, read_pose2d, write_pose2d
from app.session import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_REVIEW,
    SessionStore,
    export_session,
    validate_session_builtin,
)
from app.session.export import core_validator_available, validate_with_core
from app.ui.player import ClipPlayer, PlayerWidget
from app.ui.settings import AppSettings
from app.ui.theme import mono_font
from app.ui.widgets import SectionHeader, TelemetryValue, block_widget

_FILTER_ALL = "全部"
_FILTERS = [_FILTER_ALL, STATUS_PASS, STATUS_FAIL, STATUS_REVIEW]

MEDIAPIPE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)


class _PoseWorker(QThread):
    """骨架推理线程（PRD F7：后台跑，不阻塞 UI）。"""

    progressed = Signal(int, int)   # (已完成, 总数)
    finished_ok = Signal(object)    # list[PoseFrame]
    failed = Signal(str)

    def __init__(self, estimator: PoseEstimator, player: ClipPlayer, eye: str) -> None:
        super().__init__()
        self._estimator = estimator
        self.model_name = estimator.model_name
        self._player = player
        self._eye = eye

    def run(self) -> None:
        try:
            frames: list[PoseFrame] = []
            total = self._player.frame_count
            for i in range(total):
                if self.isInterruptionRequested():
                    return
                gray = self._player.frame(i, self._eye)
                if gray is None:
                    continue
                ts_ms = i / self._player.fps * 1000.0
                frames.append(self._estimator.estimate(gray, i, ts_ms))
                if i % 10 == 0:
                    self.progressed.emit(i + 1, total)
            self.finished_ok.emit(frames)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self._estimator.close()


class ReviewPage(QWidget):
    """素材审核：筛选列表、逐帧回放、骨架叠加与手动修正、修剪、三段式标记、批量导出。"""

    def __init__(self, settings: AppSettings, store: SessionStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self._player: ClipPlayer | None = None
        self._record: dict | None = None
        self._frame_pos = 0
        self._playing = False
        self._pose_frames: list[PoseFrame] | None = None
        self._pose_model = ""
        self._pose_fps = 0.0
        self._undo_stack: list[tuple[int, int]] = []  # (帧位置, 关键点索引)
        self._pose_dirty = False
        self._pose_worker: _PoseWorker | None = None
        self._build_ui()
        self.refresh_list()

    # ---- UI 组装 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(1)

        top = block_widget(SectionHeader("[ REVIEW ]"))
        top.layout().setContentsMargins(10, 6, 10, 6)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(1)
        root.addLayout(body, stretch=1)

        # 左：筛选 + 素材列表 + 批量导出
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(SectionHeader(">>> CLIPS"))
        self.combo_filter = QComboBox()
        self.combo_filter.setFont(mono_font(10))
        self.combo_filter.addItems(_FILTERS)
        self.combo_filter.currentTextChanged.connect(lambda _t: self.refresh_list())
        left_layout.addWidget(self.combo_filter)
        self.list_clips = QListWidget()
        self.list_clips.setFont(mono_font(10))
        self.list_clips.currentItemChanged.connect(self._on_item_selected)
        left_layout.addWidget(self.list_clips, stretch=1)
        self.btn_export = QPushButton("批量导出（合格素材）")
        self.btn_export.setObjectName("primary")
        self.btn_export.clicked.connect(self.export_passed)
        left_layout.addWidget(self.btn_export)
        body.addWidget(block_widget(left), stretch=1)

        # 右：回放 + 操作
        right = QVBoxLayout()
        right.setSpacing(1)
        body.addLayout(right, stretch=3)

        self.player = PlayerWidget()
        self.player.keypoint_moved.connect(self.correct_keypoint)
        right.addWidget(block_widget(self.player), stretch=1)

        # 走带控制
        transport = QWidget()
        tp = QHBoxLayout(transport)
        tp.setContentsMargins(0, 0, 0, 0)
        tp.setSpacing(8)
        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev = QPushButton("◀ -1")
        self.btn_next = QPushButton("+1 ▶")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._on_slider)
        self.label_frame = QLabel("FRAME --/--")
        self.label_frame.setFont(mono_font(10, letter_spacing=1.5))
        self.combo_eye = QComboBox()
        self.combo_eye.setFont(mono_font(10))
        self.combo_eye.addItems(["左目", "右目"])
        self.combo_eye.currentIndexChanged.connect(lambda _i: self._show_frame())
        tp.addWidget(self.btn_play)
        tp.addWidget(self.btn_prev)
        tp.addWidget(self.btn_next)
        tp.addWidget(self.slider, stretch=1)
        tp.addWidget(self.label_frame)
        tp.addWidget(self.combo_eye)
        right.addWidget(block_widget(transport))

        # 操作区：修剪 / 骨架 / 标记
        ops = QWidget()
        ops_layout = QHBoxLayout(ops)
        ops_layout.setContentsMargins(0, 0, 0, 0)
        ops_layout.setSpacing(16)

        trim_box = QVBoxLayout()
        trim_box.addWidget(SectionHeader(">>> TRIM"))
        trim_row = QHBoxLayout()
        self.btn_trim_start = QPushButton("设为起点")
        self.btn_trim_end = QPushButton("设为终点")
        self.btn_trim_start.clicked.connect(lambda: self._set_trim_point("start"))
        self.btn_trim_end.clicked.connect(lambda: self._set_trim_point("end"))
        trim_row.addWidget(self.btn_trim_start)
        trim_row.addWidget(self.btn_trim_end)
        trim_box.addLayout(trim_row)
        self.label_trim = QLabel("[--, --]")
        self.label_trim.setFont(mono_font(10, letter_spacing=1.5))
        trim_box.addWidget(self.label_trim)
        self.btn_trim_save = QPushButton("保存修剪")
        self.btn_trim_save.clicked.connect(self.save_trim)
        trim_box.addWidget(self.btn_trim_save)
        ops_layout.addLayout(trim_box)

        pose_box = QVBoxLayout()
        pose_box.addWidget(SectionHeader(">>> POSE"))
        self.btn_run_pose = QPushButton("跑骨架（整段）")
        self.btn_run_pose.clicked.connect(self.run_pose)
        pose_box.addWidget(self.btn_run_pose)
        pose_row = QHBoxLayout()
        self.btn_rerun_frame = QPushButton("重跑单帧")
        self.btn_undo = QPushButton("撤销修正")
        self.btn_rerun_frame.clicked.connect(self.rerun_current_frame)
        self.btn_undo.clicked.connect(self.undo_correction)
        pose_row.addWidget(self.btn_rerun_frame)
        pose_row.addWidget(self.btn_undo)
        pose_box.addLayout(pose_row)
        pose_row2 = QHBoxLayout()
        self.chk_pose = QCheckBox("骨架叠加")
        self.chk_pose.setChecked(True)
        self.chk_pose.setFont(mono_font(10))
        self.chk_pose.toggled.connect(self.player.set_pose_visible)
        self.btn_save_pose = QPushButton("保存骨架")
        self.btn_save_pose.clicked.connect(self.save_pose)
        pose_row2.addWidget(self.chk_pose)
        pose_row2.addWidget(self.btn_save_pose)
        pose_box.addLayout(pose_row2)
        self.label_pose = TelemetryValue("骨架", "未跑")
        pose_box.addWidget(self.label_pose)
        ops_layout.addLayout(pose_box)

        mark_box = QVBoxLayout()
        mark_box.addWidget(SectionHeader(">>> MARK"))
        self.btn_pass = QPushButton("合格")
        self.btn_fail = QPushButton("不合格")
        self.btn_review = QPushButton("待复核")
        self.btn_pass.clicked.connect(lambda: self.mark(STATUS_PASS))
        self.btn_fail.clicked.connect(lambda: self.mark(STATUS_FAIL))
        self.btn_review.clicked.connect(lambda: self.mark(STATUS_REVIEW))
        for b in (self.btn_pass, self.btn_fail, self.btn_review):
            mark_box.addWidget(b)
        ops_layout.addLayout(mark_box)
        ops_layout.addStretch(1)
        right.addWidget(block_widget(ops))

        self.status_line = QLabel("选择左侧素材开始审核")
        self.status_line.setObjectName("dim")
        self.status_line.setFont(mono_font(10, letter_spacing=1.5))
        root.addWidget(block_widget(self.status_line))

        # 快捷键：←/→ 逐帧，空格 播放/暂停（PRD F8 帧步进）
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.step(1))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_play)

        self._play_timer_id: int | None = None

    # ---- 素材列表 ----

    def refresh_list(self) -> None:
        status = self.combo_filter.currentText()
        records = self.store.list(None if status == _FILTER_ALL else status)
        self.list_clips.clear()
        for r in records:
            pose_mark = "P" if (self.store.root / r["clip_dir"] / "pose2d.json").is_file() else "-"
            text = (
                f"#{r['seq']:03d} {r['session_id'][:14]}… "
                f"{r['frame_count']}F {r['frame_count'] / max(r['fps'], 1):.1f}S "
                f"[{r['status']}] 骨架:{pose_mark}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r["session_id"])
            self.list_clips.addItem(item)

    def _on_item_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is not None:
            self.load_record(item.data(Qt.ItemDataRole.UserRole))

    # ---- 回放 ----

    def load_record(self, session_id: str) -> None:
        """加载一段素材：视频帧 + 已有骨架。"""
        self.stop_play()
        if self._player is not None:
            self._player.close()
        record = self.store.get(session_id)
        self._record = record
        self._player = ClipPlayer(
            self.store.root / record["clip_dir"], record["frame_count"], record["fps"]
        )
        loaded = self._player.load_pose()
        if loaded is not None:
            self._pose_model, self._pose_fps, self._pose_frames = loaded
            self.label_pose.set_value(f"已加载 {len(self._pose_frames)}F")
        else:
            self._pose_frames = None
            self.label_pose.set_value("未跑")
        self._undo_stack.clear()
        self._pose_dirty = False
        trim = record["trim"]
        self.label_trim.setText(f"[{trim['start_frame']}, {trim['end_frame']}]")
        self.slider.setRange(0, max(0, record["frame_count"] - 1))
        self._frame_pos = trim["start_frame"]
        self.slider.setValue(self._frame_pos)
        self._show_frame()
        self.status_line.setText(f">>> SESSION {record['seq']:03d} 已加载")

    def _current_eye(self) -> str:
        return "left" if self.combo_eye.currentIndex() == 0 else "right"

    def _show_frame(self) -> None:
        if self._player is None or self._record is None:
            return
        gray = self._player.frame(self._frame_pos, self._current_eye())
        pose = None
        if self._pose_frames and 0 <= self._frame_pos < len(self._pose_frames):
            pose = self._pose_frames[self._frame_pos]
        self.player.set_frame_data(gray, pose)
        total = self._record["frame_count"]
        self.label_frame.setText(f"FRAME {self._frame_pos:04d}/{total - 1:04d}")

    def step(self, delta: int) -> None:
        if self._record is None:
            return
        total = self._record["frame_count"]
        self._frame_pos = min(max(self._frame_pos + delta, 0), total - 1)
        self.slider.blockSignals(True)
        self.slider.setValue(self._frame_pos)
        self.slider.blockSignals(False)
        self._show_frame()

    def _on_slider(self, value: int) -> None:
        if self._record is None:
            return
        self._frame_pos = value
        self._show_frame()

    def toggle_play(self) -> None:
        if self._record is None:
            return
        if self._playing:
            self.stop_play()
        else:
            interval = max(1, int(1000 / max(self._record["fps"], 1)))
            self._play_timer_id = self.startTimer(interval)
            self._playing = True
            self.btn_play.setText("暂停")

    def stop_play(self) -> None:
        if self._play_timer_id is not None:
            self.killTimer(self._play_timer_id)
            self._play_timer_id = None
        self._playing = False
        self.btn_play.setText("播放")

    def timerEvent(self, event) -> None:  # noqa: N802
        if self._record is None:
            self.stop_play()
            return
        if self._frame_pos >= self._record["frame_count"] - 1:
            self.stop_play()
            return
        self.step(1)

    # ---- 骨架 ----

    def _make_estimator(self) -> PoseEstimator | None:
        """按设置构建估计器：配置 MediaPipe 模型优先，缺失时提示下载并回退 Stub。"""
        from app.pose import MediaPipePoseEstimator

        model_path = self.settings.pose_model_path
        if model_path:
            if not Path(model_path).is_file():
                QMessageBox.warning(
                    self, "模型文件缺失",
                    f"MediaPipe 模型文件不存在：\n{model_path}\n\n"
                    f"请下载 pose_landmarker_lite.task：\n{MEDIAPIPE_MODEL_URL}\n\n"
                    "本次将使用 StubPoseEstimator（确定性测试骨架）代替。",
                )
            elif MediaPipePoseEstimator.is_available():
                try:
                    return MediaPipePoseEstimator(model_path)
                except Exception as e:
                    QMessageBox.warning(self, "骨架初始化失败", f"{e}\n\n回退到 StubPoseEstimator。")
            else:
                QMessageBox.warning(
                    self, "mediapipe 不可用",
                    "未安装 mediapipe（pip install -e \".[pose]\"），回退到 StubPoseEstimator。",
                )
        return StubPoseEstimator()

    def run_pose(self) -> None:
        """整段跑骨架（后台线程）。"""
        if self._player is None or self._pose_worker is not None:
            return
        estimator = self._make_estimator()
        if estimator is None:
            return
        self._pose_worker = _PoseWorker(estimator, self._player, "left")
        self._pose_worker.progressed.connect(
            lambda done, total: self.label_pose.set_value(f"推理 {done}/{total}")
        )
        self._pose_worker.finished_ok.connect(self._on_pose_done)
        self._pose_worker.failed.connect(self._on_pose_failed)
        self._pose_worker.finished.connect(self._on_pose_worker_finished)
        self.btn_run_pose.setEnabled(False)
        self._pose_worker.start()

    def _on_pose_done(self, frames: list) -> None:
        self._pose_frames = frames
        if self._pose_worker is not None:
            self._pose_model = self._pose_worker.model_name
        self._pose_fps = self._record["fps"] if self._record else 0.0
        self._pose_dirty = True
        self.label_pose.set_value(f"已跑 {len(frames)}F（未保存）")
        self._show_frame()

    def _on_pose_failed(self, message: str) -> None:
        self.label_pose.set_value("失败")
        self.status_line.setText(f">>> 骨架推理失败: {message}")

    def _on_pose_worker_finished(self) -> None:
        self._pose_worker = None
        self.btn_run_pose.setEnabled(True)

    def rerun_current_frame(self) -> None:
        """重跑当前帧骨架（覆盖该帧，含手动修正）。"""
        if self._player is None or self._pose_frames is None:
            return
        if not (0 <= self._frame_pos < len(self._pose_frames)):
            return
        estimator = self._make_estimator()
        if estimator is None:
            return
        gray = self._player.frame(self._frame_pos, "left")
        if gray is None:
            return
        ts_ms = self._frame_pos / max(self._record["fps"], 1) * 1000.0
        self._pose_frames[self._frame_pos] = estimator.estimate(gray, self._frame_pos, ts_ms)
        estimator.close()
        self._pose_dirty = True
        self.label_pose.set_value("单帧已重跑（未保存）")
        self._show_frame()

    # ---- 手动修正（PRD F8） ----

    def correct_keypoint(self, kp_idx: int, x: float, y: float) -> None:
        """拖动落点：调核心 PoseFrame.correct 落账（保留自动原值）。"""
        if self._pose_frames is None or not (0 <= self._frame_pos < len(self._pose_frames)):
            return
        frame = self._pose_frames[self._frame_pos]
        frame.correct(kp_idx, x, y)
        self._undo_stack.append((self._frame_pos, kp_idx))
        self._pose_dirty = True
        self.label_pose.set_value("已修正（未保存）")
        self._show_frame()

    def undo_correction(self) -> None:
        """撤销最近一次单点修正：从 kp.auto 恢复自动原值。"""
        if not self._undo_stack or self._pose_frames is None:
            return
        frame_pos, kp_idx = self._undo_stack.pop()
        kp = self._pose_frames[frame_pos].keypoints[kp_idx]
        if kp.manual and kp.auto is not None:
            kp.x = kp.auto["x"]
            kp.y = kp.auto["y"]
            kp.visibility = kp.auto["visibility"]
            kp.manual = False
            kp.auto = None
        self._pose_dirty = True
        self._frame_pos = frame_pos
        self.slider.blockSignals(True)
        self.slider.setValue(frame_pos)
        self.slider.blockSignals(False)
        self.label_pose.set_value("已撤销（未保存）")
        self._show_frame()

    def save_pose(self) -> bool:
        """保存骨架回 clip_dir/pose2d.json。"""
        if self._player is None or self._pose_frames is None:
            return False
        model = self._pose_model or StubPoseEstimator.model_name
        fps = self._pose_fps or (self._record["fps"] if self._record else 0.0)
        write_pose2d(self._player.clip_dir / "pose2d.json", model, fps, self._pose_frames)
        self._pose_model, self._pose_fps = model, fps
        self._pose_dirty = False
        self.label_pose.set_value(f"已保存 {len(self._pose_frames)}F")
        self.status_line.setText(">>> pose2d.json 已保存")
        self.refresh_list()
        return True

    # ---- 修剪（PRD F9） ----

    def _set_trim_point(self, which: str) -> None:
        if self._record is None:
            return
        trim = dict(self._record["trim"])
        if which == "start":
            trim["start_frame"] = min(self._frame_pos, trim["end_frame"])
        else:
            trim["end_frame"] = max(self._frame_pos, trim["start_frame"])
        self._record["trim"] = trim
        self.label_trim.setText(f"[{trim['start_frame']}, {trim['end_frame']}]")

    def save_trim(self) -> bool:
        if self._record is None:
            return False
        trim = self._record["trim"]
        self.store.set_trim(
            self._record["session_id"], trim["start_frame"], trim["end_frame"]
        )
        self.status_line.setText(
            f">>> 修剪已保存 [{trim['start_frame']}, {trim['end_frame']}]"
        )
        return True

    # ---- 标记（PRD F10） ----

    def mark(self, status: str) -> None:
        if self._record is None:
            return
        self.store.mark(self._record["session_id"], status)
        self._record["status"] = status
        self.status_line.setText(f">>> 已标记: {status}")
        self.refresh_list()

    # ---- 批量导出（PRD F10） ----

    def export_passed(self) -> list[Path]:
        """导出全部合格素材到 <存储根>/exports/，弹窗报告路径与校验结果。"""
        records = self.store.list(STATUS_PASS)
        if not records:
            self.status_line.setText(">>> 无合格素材可导出")
            QMessageBox.information(self, "批量导出", "没有标记为「合格」的素材。")
            return []
        out_root = self.store.root / "exports"
        exported: list[Path] = []
        errors: list[str] = []
        for record in records:
            try:
                exported.append(self._export_one(record, out_root))
            except Exception as e:
                errors.append(f"{record['session_id']}: {e}")
        core_ok = core_validator_available()
        lines = [f"已导出 {len(exported)} 段到:\n{out_root}", ""]
        for d in exported:
            data = json.loads((d / "session.json").read_text(encoding="utf-8"))
            builtin = validate_session_builtin(data)
            core = validate_with_core(d / "session.json") if core_ok else None
            check = "内置校验通过" if not builtin else f"内置校验失败 {builtin}"
            if core is not None:
                check += "；core 全量校验通过" if not core else f"；core 校验失败 {core}"
            lines.append(f"{d.name}: {check}")
        if errors:
            lines += ["", "失败:"] + errors
        QMessageBox.information(self, "批量导出", "\n".join(lines))
        self.status_line.setText(f">>> 导出完成 {len(exported)} 段 → {out_root}")
        return exported

    def _export_one(self, record: dict, out_root: Path) -> Path:
        """单段导出：按修剪区间读回帧序列，附骨架（若有）。"""
        from app.detect.state_machine import Clip
        from app.capture import RingBuffer

        player = ClipPlayer(
            self.store.root / record["clip_dir"], record["frame_count"], record["fps"]
        )
        try:
            trim = record["trim"]
            start, end = trim["start_frame"], trim["end_frame"]
            items = []
            for i in range(start, end + 1):
                left = player.frame(i, "left")
                right = player.frame(i, "right")
                if left is None:
                    break
                items.append((i, int(i / record["fps"] * 1e9), left,
                              right if right is not None else left))
            pose_frames = None
            pose_model = None
            pose_path = player.clip_dir / "pose2d.json"
            if pose_path.is_file():
                pose_model, _fps, all_frames = read_pose2d(pose_path)
                sliced = all_frames[start : end + 1]
                # 帧序号/时间戳按导出片段重排
                for j, pf in enumerate(sliced):
                    pf.frame_index = j
                    pf.timestamp_ms = j / record["fps"] * 1000.0
                pose_frames = sliced
            dummy = Clip(seq=record["seq"], start_idx=start, end_idx=end,
                         trigger_idx=start, buffer=RingBuffer(1.0, record["fps"]))
            return export_session(
                dummy, out_root,
                pose_frames=pose_frames, pose_model=pose_model,
                session_id=record["session_id"], frames=items,
            )
        finally:
            player.close()

    def shutdown(self) -> None:
        self.stop_play()
        if self._pose_worker is not None:
            self._pose_worker.requestInterruption()
            self._pose_worker.wait(3000)
        if self._player is not None:
            self._player.close()
