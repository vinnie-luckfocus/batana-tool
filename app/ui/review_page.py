"""审核页（PRD F7–F10 界面）：素材列表 + 回放 + 骨架叠加/修正 + 修剪 + 标记 + 导出。"""

from __future__ import annotations

import json
import shutil
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
    QStyle,
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
from app.session.export import resolve_core_repo, validate_with_core
from app.ui.player import ClipPlayer, PlayerWidget
from app.ui.settings import AppSettings
from app.ui.theme import mono_font, ui_font
from app.ui.widgets import EmptyStateView, SectionHeader, TelemetryValue, card_widget

_FILTER_ALL = "全部"
_FILTERS = [_FILTER_ALL, STATUS_PASS, STATUS_FAIL, STATUS_REVIEW]

MEDIAPIPE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)

# M2 骨架目选择：下拉文案 → 目列表（双目顺序固定先左后右）
_POSE_EYE_OPTIONS = [("左目", ["left"]), ("右目", ["right"]), ("双目", ["left", "right"])]

# L3 回放速度档：下拉文案 → 倍率
_SPEED_OPTIONS = [("0.25×", 0.25), ("0.5×", 0.5), ("1×", 1.0)]

# 列表项数据角色：UserRole = session_id，UserRole+1 = 审核状态
_ROLE_STATUS = Qt.ItemDataRole.UserRole + 1

# M4 磁盘预检安全系数（编码后实际更小，按原始灰度估算偏保守）
_EXPORT_DISK_SAFETY = 1.2


def pose_path_for_eye(clip_dir: Path, eye: str) -> Path:
    """骨架文件按目分存：左目沿用 pose2d.json（契约既有结构），右目 pose2d_right.json。"""
    return clip_dir / ("pose2d.json" if eye == "left" else f"pose2d_{eye}.json")


class _PoseWorker(QThread):
    """骨架推理线程（PRD F7：后台跑，不阻塞 UI）。支持单目/双目（M2）。"""

    progressed = Signal(int, int)   # (已完成, 总数)
    finished_ok = Signal(object)    # dict[eye, list[PoseFrame]]
    failed = Signal(str)

    def __init__(self, estimator: PoseEstimator, player: ClipPlayer, eyes: list[str]) -> None:
        super().__init__()
        self._estimator = estimator
        self.model_name = estimator.model_name
        self._player = player
        self._eyes = eyes

    def run(self) -> None:
        try:
            total = self._player.frame_count * len(self._eyes)
            done = 0
            frames_by_eye: dict[str, list[PoseFrame]] = {}
            for eye in self._eyes:
                frames: list[PoseFrame] = []
                for i in range(self._player.frame_count):
                    if self.isInterruptionRequested():
                        return
                    gray = self._player.frame(i, eye)
                    if gray is None:
                        continue
                    ts_ms = i / self._player.fps * 1000.0
                    frames.append(self._estimator.estimate(gray, i, ts_ms))
                    done += 1
                    if done % 10 == 0:
                        self.progressed.emit(done, total)
                frames_by_eye[eye] = frames
            self.finished_ok.emit(frames_by_eye)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self._estimator.close()


class _ExportWorker(QThread):
    """批量导出线程（M4：主线程不阻塞，逐段报进度）。"""

    progressed = Signal(int, int)   # (已完成, 总数)
    finished_all = Signal(object)   # (exported: list[Path], errors: list[str], overwritten: list[str])

    def __init__(self, page: "ReviewPage", records: list[dict], out_root: Path) -> None:
        super().__init__(page)
        self._page = page
        self._records = records
        self._out_root = out_root

    def run(self) -> None:
        exported: list[Path] = []
        errors: list[str] = []
        overwritten: list[str] = []
        total = len(self._records)
        for i, record in enumerate(self._records):
            if self.isInterruptionRequested():
                break
            try:
                existed = (self._out_root / "sessions" / record["session_id"]).exists()
                out = self._page._export_one(record, self._out_root)
                exported.append(out)
                if existed:
                    overwritten.append(record["session_id"])
            except Exception as e:
                errors.append(f"{record['session_id']}: {e}")
            self.progressed.emit(i + 1, total)
        self.finished_all.emit((exported, errors, overwritten))


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
        self._pose_frames_by_eye: dict[str, list[PoseFrame]] = {}
        self._pose_eye = "left"
        self._pose_model = ""
        self._pose_fps = 0.0
        self._undo_stack: list[tuple[int, int]] = []  # (帧位置, 关键点索引)
        self._pose_dirty = False
        self._trim_dirty = False
        self._pose_worker: _PoseWorker | None = None
        self._export_worker: _ExportWorker | None = None
        self._speed = 1.0
        self._build_ui()
        self.refresh_list()

    # ---- UI 组装 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, stretch=1)

        # 左：筛选 + 素材列表 + 批量导出（访达侧栏观感：sidebar 材质毛玻璃面板）
        left = QWidget()
        left.setMinimumWidth(320)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(SectionHeader("素材"))
        self.combo_filter = QComboBox()
        self.combo_filter.addItems(_FILTERS)
        self.combo_filter.currentTextChanged.connect(lambda _t: self.refresh_list())
        left_layout.addWidget(self.combo_filter)
        self.list_clips = QListWidget()
        self.list_clips.currentItemChanged.connect(self._on_item_selected)
        left_layout.addWidget(self.list_clips, stretch=1)
        # 空状态引导视图：无素材时覆盖在列表上（图标位 + 标题 + 说明 + 行动按钮）
        self.list_empty = EmptyStateView(
            "暂无素材",
            "先到「采集」页录制挥棒片段，\n保存后会出现在这里。",
            icon=self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
            action_text="前往采集页",
            on_action=self._goto_capture,
            parent=self.list_clips.viewport(),
        )
        self.btn_export = QPushButton("批量导出（合格素材）")
        self.btn_export.setObjectName("primary")
        self.btn_export.clicked.connect(self.export_passed)
        left_layout.addWidget(self.btn_export)
        self.btn_delete = QPushButton("删除选中素材")
        self.btn_delete.clicked.connect(self.delete_current)
        left_layout.addWidget(self.btn_delete)
        sidebar = QWidget()
        sidebar.setObjectName("sidebarPanel")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.addWidget(left)
        # 侧栏透明：透出主窗口 underWindowBackground 毛玻璃（访达侧栏观感）。
        # 不给子面板单独挂 NSVisualEffectView——子部件 winId() 强制 native 后
        # Qt 不再绘制其子树（列表/按钮会整体消失），vibrancy 只挂顶层窗口。
        body.addWidget(sidebar, stretch=1)

        # 右：回放 + 操作
        right = QVBoxLayout()
        right.setSpacing(12)
        body.addLayout(right, stretch=3)

        self.player = PlayerWidget()
        self.player.keypoint_moved.connect(self.correct_keypoint)
        right.addWidget(card_widget(self.player), stretch=1)

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
        self.label_frame = QLabel("帧 --/--")
        self.label_frame.setFont(mono_font(12))
        self.btn_trigger = QPushButton("跳到触发帧")
        self.btn_trigger.clicked.connect(self.jump_to_trigger)
        self.combo_speed = QComboBox()
        for name, _v in _SPEED_OPTIONS:
            self.combo_speed.addItem(name)
        self.combo_speed.setCurrentIndex(2)  # 默认 1×
        self.combo_speed.currentIndexChanged.connect(self._on_speed_changed)
        self.combo_eye = QComboBox()
        self.combo_eye.addItems(["左目", "右目"])
        self.combo_eye.currentIndexChanged.connect(lambda _i: self._on_view_eye_changed())
        tp.addWidget(self.btn_play)
        tp.addWidget(self.btn_prev)
        tp.addWidget(self.btn_next)
        tp.addWidget(self.slider, stretch=1)
        tp.addWidget(self.label_frame)
        tp.addWidget(self.btn_trigger)
        tp.addWidget(self.combo_speed)
        tp.addWidget(self.combo_eye)
        right.addWidget(card_widget(transport, shadow=True))

        # 操作区：修剪 / 骨架 / 标记
        ops = QWidget()
        ops_layout = QHBoxLayout(ops)
        ops_layout.setContentsMargins(0, 0, 0, 0)
        ops_layout.setSpacing(24)

        trim_box = QVBoxLayout()
        trim_box.setSpacing(8)
        trim_box.addWidget(SectionHeader("修剪"))
        trim_row = QHBoxLayout()
        self.btn_trim_start = QPushButton("设为起点")
        self.btn_trim_end = QPushButton("设为终点")
        self.btn_trim_start.clicked.connect(lambda: self._set_trim_point("start"))
        self.btn_trim_end.clicked.connect(lambda: self._set_trim_point("end"))
        trim_row.addWidget(self.btn_trim_start)
        trim_row.addWidget(self.btn_trim_end)
        trim_box.addLayout(trim_row)
        self.label_trim = QLabel("[--, --]")
        self.label_trim.setFont(mono_font(12))
        trim_box.addWidget(self.label_trim)
        self.btn_trim_save = QPushButton("保存修剪")
        self.btn_trim_save.clicked.connect(self.save_trim)
        trim_box.addWidget(self.btn_trim_save)
        trim_box.addStretch(1)
        ops_layout.addLayout(trim_box)

        pose_box = QVBoxLayout()
        pose_box.setSpacing(8)
        pose_box.addWidget(SectionHeader("骨架"))
        pose_eye_row = QHBoxLayout()
        pose_eye_label = QLabel("目")
        pose_eye_label.setObjectName("dim")
        self.combo_pose_eye = QComboBox()
        for name, _eyes in _POSE_EYE_OPTIONS:
            self.combo_pose_eye.addItem(name)
        pose_eye_row.addWidget(pose_eye_label)
        pose_eye_row.addWidget(self.combo_pose_eye, stretch=1)
        pose_box.addLayout(pose_eye_row)
        self.btn_run_pose = QPushButton("跑骨架（整段）")
        self.btn_run_pose.setObjectName("primary")
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
        self.chk_pose.toggled.connect(self.player.set_pose_visible)
        self.btn_save_pose = QPushButton("保存骨架")
        self.btn_save_pose.clicked.connect(self.save_pose)
        pose_row2.addWidget(self.chk_pose)
        pose_row2.addWidget(self.btn_save_pose)
        pose_box.addLayout(pose_row2)
        self.label_pose = TelemetryValue("骨架", "未跑")
        pose_box.addWidget(self.label_pose)
        pose_box.addStretch(1)
        ops_layout.addLayout(pose_box)

        mark_box = QVBoxLayout()
        mark_box.setSpacing(8)
        mark_box.addWidget(SectionHeader("标记"))
        self.btn_pass = QPushButton("合格")
        self.btn_fail = QPushButton("不合格")
        self.btn_review = QPushButton("待复核")
        self.btn_pass.clicked.connect(lambda: self.mark(STATUS_PASS))
        self.btn_fail.clicked.connect(lambda: self.mark(STATUS_FAIL))
        self.btn_review.clicked.connect(lambda: self.mark(STATUS_REVIEW))
        for b in (self.btn_pass, self.btn_fail, self.btn_review):
            mark_box.addWidget(b)
        mark_box.addStretch(1)
        ops_layout.addLayout(mark_box)
        ops_layout.addStretch(1)
        right.addWidget(card_widget(ops, shadow=True))

        self.status_line = QLabel("选择左侧素材开始审核")
        self.status_line.setObjectName("dim")
        self.status_line.setFont(ui_font(12))
        root.addWidget(self.status_line)

        # 快捷键：←/→ 逐帧，Shift+←/→ ±10 帧，空格 播放/暂停，Ctrl+Z 撤销修正
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.step(1))
        QShortcut(QKeySequence("Shift+Left"), self, activated=lambda: self.step(-10))
        QShortcut(QKeySequence("Shift+Right"), self, activated=lambda: self.step(10))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_play)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo_correction)

        self._play_timer_id: int | None = None

    # ---- 素材列表 ----

    def refresh_list(self) -> None:
        status = self.combo_filter.currentText()
        records = self.store.list(None if status == _FILTER_ALL else status)
        self.list_clips.clear()
        for r in records:
            clip_dir = self.store.root / r["clip_dir"]
            pose_marks = "".join(
                eye[0].upper() for eye in ("left", "right")
                if pose_path_for_eye(clip_dir, eye).is_file()
            )
            text = (
                f"#{r['seq']:03d} {r['session_id'][:14]}… "
                f"{r['frame_count']}F {r['frame_count'] / max(r['fps'], 1):.1f}S "
                f"[{r['status']}] 骨架:{pose_marks or '-'}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r["session_id"])
            item.setData(_ROLE_STATUS, r["status"])
            self.list_clips.addItem(item)
        self._update_empty_hint()

    def _update_empty_hint(self) -> None:
        """空状态引导：列表为空时显示引导视图，否则隐藏。"""
        self.list_empty.setVisible(self.list_clips.count() == 0)
        self.list_empty.setGeometry(self.list_clips.viewport().rect())
        self.list_empty.raise_()

    def _goto_capture(self) -> None:
        """空态行动按钮：切回采集页。"""
        window = self.window()
        if hasattr(window, "stack"):
            window.stack.setCurrentIndex(0)
            window._tabs[0].setChecked(True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "list_empty"):
            self._update_empty_hint()

    def _on_item_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is not None:
            self.load_record(item.data(Qt.ItemDataRole.UserRole))

    # ---- 回放 ----

    def load_record(self, session_id: str) -> bool:
        """加载一段素材：视频帧 + 已有骨架。有未保存修改时先确认（H3）。"""
        if self._record is not None and self._record["session_id"] == session_id:
            return True
        if not self._confirm_unsaved():
            self._restore_list_selection()
            return False
        self.stop_play()
        if self._player is not None:
            self._player.close()
        record = self.store.get(session_id)
        self._record = record
        self._player = ClipPlayer(
            self.store.root / record["clip_dir"], record["frame_count"], record["fps"]
        )
        self._load_pose_for_eye(self._current_eye())
        self._undo_stack.clear()
        self._pose_dirty = False
        self._trim_dirty = False
        trim = record["trim"]
        self.label_trim.setText(f"[{trim['start_frame']}, {trim['end_frame']}]")
        self.slider.setRange(0, max(0, record["frame_count"] - 1))
        self._frame_pos = trim["start_frame"]
        self.slider.setValue(self._frame_pos)
        self._show_frame()
        self.status_line.setText(f"第 {record['seq']:03d} 段已加载")
        return True

    def _load_pose_for_eye(self, eye: str) -> None:
        """按目载入骨架：左目 pose2d.json（既有结构），右目 pose2d_right.json。"""
        self._pose_frames_by_eye = {}
        self._pose_eye = eye
        path = pose_path_for_eye(self._player.clip_dir, eye)
        if path.is_file():
            self._pose_model, self._pose_fps, self._pose_frames = read_pose2d(path)
            self._pose_frames_by_eye[eye] = self._pose_frames
            self.label_pose.set_value(f"已加载 {len(self._pose_frames)}F（{eye}）")
        else:
            self._pose_frames = None
            self.label_pose.set_value("未跑")

    # ---- 未保存修改确认（H3） ----

    def _has_unsaved(self) -> bool:
        return self._pose_dirty or self._trim_dirty

    def _confirm_unsaved(self) -> bool:
        """切换素材前检查脏标志：保存 / 放弃 / 取消。返回是否继续切换。"""
        if self._record is None or not self._has_unsaved():
            return True
        choice = QMessageBox.question(
            self, "未保存的修改",
            "当前素材有未保存的骨架修正或修剪。\n保存后再切换，还是放弃修改？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            if self._pose_dirty and self._pose_frames is not None:
                self.save_pose()
            if self._trim_dirty:
                self.save_trim()
        return True

    def _restore_list_selection(self) -> None:
        """取消切换后把列表选择还原到当前已加载素材（避免选择漂移）。"""
        self.list_clips.blockSignals(True)
        if self._record is None:
            self.list_clips.clearSelection()
            self.list_clips.setCurrentItem(None)
        else:
            sid = self._record["session_id"]
            for row in range(self.list_clips.count()):
                item = self.list_clips.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == sid:
                    self.list_clips.setCurrentRow(row)
                    break
        self.list_clips.blockSignals(False)

    def _current_eye(self) -> str:
        return "left" if self.combo_eye.currentIndex() == 0 else "right"

    def _on_view_eye_changed(self) -> None:
        """切换查看目：载入该目已存骨架（当前有未保存修改时保留内存版本）。"""
        if self._player is not None and not self._pose_dirty:
            eye = self._current_eye()
            if eye != self._pose_eye:
                self._load_pose_for_eye(eye)
        self._show_frame()

    def jump_to_trigger(self) -> None:
        """H2：跳到挥棒触发帧（trigger_idx 来自索引）。"""
        if self._record is None:
            return
        trigger = self._record.get("trigger_idx")
        if trigger is None:
            self.status_line.setText("本段无触发帧记录（手动片段）")
            return
        self._frame_pos = min(max(int(trigger), 0), self._record["frame_count"] - 1)
        self.slider.blockSignals(True)
        self.slider.setValue(self._frame_pos)
        self.slider.blockSignals(False)
        self._show_frame()

    def _on_speed_changed(self, index: int) -> None:
        """L3：回放速度 0.25×/0.5×/1×；播放中切换即时生效。"""
        self._speed = _SPEED_OPTIONS[index][1]
        if self._playing:
            self.stop_play()
            self.toggle_play()

    def _show_frame(self) -> None:
        if self._player is None or self._record is None:
            return
        gray = self._player.frame(self._frame_pos, self._current_eye())
        pose = None
        if self._pose_frames and 0 <= self._frame_pos < len(self._pose_frames):
            pose = self._pose_frames[self._frame_pos]
        self.player.set_frame_data(gray, pose)
        total = self._record["frame_count"]
        self.label_frame.setText(f"帧 {self._frame_pos:04d}/{total - 1:04d}")

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
            interval = max(1, int(1000 / max(self._record["fps"], 1) / self._speed))
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
        """整段跑骨架（后台线程），目别由下拉选择（左/右/双）。"""
        if self._player is None or self._pose_worker is not None:
            return
        estimator = self._make_estimator()
        if estimator is None:
            return
        eyes = _POSE_EYE_OPTIONS[self.combo_pose_eye.currentIndex()][1]
        self._pose_worker = _PoseWorker(estimator, self._player, eyes)
        self._pose_worker.progressed.connect(
            lambda done, total: self.label_pose.set_value(f"推理 {done}/{total}")
        )
        self._pose_worker.finished_ok.connect(self._on_pose_done)
        self._pose_worker.failed.connect(self._on_pose_failed)
        self._pose_worker.finished.connect(self._on_pose_worker_finished)
        self.btn_run_pose.setEnabled(False)
        self._pose_worker.start()

    def _on_pose_done(self, frames_by_eye: dict) -> None:
        self._pose_frames_by_eye = frames_by_eye
        # 编辑目标：优先当前查看目，其次左目
        eye = self._current_eye() if self._current_eye() in frames_by_eye else "left"
        self._pose_eye = eye
        self._pose_frames = frames_by_eye[eye]
        if self._pose_worker is not None:
            self._pose_model = self._pose_worker.model_name
        self._pose_fps = self._record["fps"] if self._record else 0.0
        self._pose_dirty = True
        counts = "/".join(f"{e}:{len(f)}F" for e, f in frames_by_eye.items())
        self.label_pose.set_value(f"已跑 {counts}（未保存）")
        self._show_frame()

    def _on_pose_failed(self, message: str) -> None:
        self.label_pose.set_value("失败")
        self.status_line.setText(f"骨架推理失败：{message}")

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
        gray = self._player.frame(self._frame_pos, self._pose_eye)
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
        """保存骨架：左目写 pose2d.json（既有契约结构），右目写 pose2d_right.json。"""
        frames_by_eye = self._pose_frames_by_eye
        if not frames_by_eye and self._pose_frames is not None:
            frames_by_eye = {self._pose_eye: self._pose_frames}
        if self._player is None or not frames_by_eye:
            return False
        model = self._pose_model or StubPoseEstimator.model_name
        fps = self._pose_fps or (self._record["fps"] if self._record else 0.0)
        for eye, frames in frames_by_eye.items():
            write_pose2d(pose_path_for_eye(self._player.clip_dir, eye), model, fps, frames)
        self._pose_model, self._pose_fps = model, fps
        self._pose_dirty = False
        self.label_pose.set_value(f"已保存 {'+'.join(frames_by_eye)}")
        self.status_line.setText("pose2d 骨架已保存")
        self.refresh_list()
        self._restore_list_selection()
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
        self._trim_dirty = True
        self.label_trim.setText(f"[{trim['start_frame']}, {trim['end_frame']}]")

    def save_trim(self) -> bool:
        if self._record is None:
            return False
        trim = self._record["trim"]
        self.store.set_trim(
            self._record["session_id"], trim["start_frame"], trim["end_frame"]
        )
        self._trim_dirty = False
        self.status_line.setText(
            f"修剪已保存 [{trim['start_frame']}, {trim['end_frame']}]"
        )
        return True

    # ---- 标记（PRD F10） ----

    def mark(self, status: str) -> None:
        if self._record is None:
            return
        self.store.mark(self._record["session_id"], status)
        self._record["status"] = status
        self.status_line.setText(f"已标记：{status}")
        self.refresh_list()
        # H2：标记后自动选中列表中下一条待复核
        self._select_next_pending()

    def _select_next_pending(self) -> bool:
        """选中列表里第一条待复核素材；全部审完返回 False。"""
        for row in range(self.list_clips.count()):
            item = self.list_clips.item(row)
            if item.data(_ROLE_STATUS) == STATUS_REVIEW:
                self.list_clips.setCurrentRow(row)
                return True
        self.list_clips.clearSelection()
        self.list_clips.setCurrentItem(None)
        self.status_line.setText("列表中已无待复核素材")
        return False

    # ---- 删除（H4） ----

    def delete_current(self) -> bool:
        """删除当前素材：二次确认后移除索引与素材目录（不可恢复）。"""
        if self._record is None:
            return False
        sid = self._record["session_id"]
        choice = QMessageBox.question(
            self, "删除素材",
            f"确认删除素材 {sid}？\n视频、时间戳与骨架标注将一并删除，不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return False
        self.stop_play()
        if self._player is not None:
            self._player.close()
        self._player = None
        self._record = None
        self._pose_frames = None
        self._pose_frames_by_eye = {}
        self._pose_dirty = False
        self._trim_dirty = False
        self.store.delete(sid)
        self.refresh_list()
        self._select_next_pending()
        self.status_line.setText(f"已删除素材 {sid}")
        return True

    # ---- 批量导出（PRD F10） ----

    def _estimate_export_bytes(self, records: list[dict]) -> int:
        """磁盘预检估算：总帧数 × 单目分辨率 × 2 目 × 1 字节（mono8）× 安全系数。"""
        eye_w = max(1, self.settings.capture_width // 2)
        eye_h = max(1, self.settings.capture_height)
        total_frames = sum(r["frame_count"] for r in records)
        return int(total_frames * eye_w * eye_h * 2 * _EXPORT_DISK_SAFETY)

    def export_passed(self) -> list[Path]:
        """批量导出合格素材：磁盘预检 → 后台线程导出 → 报告（重复导出注明覆盖）。"""
        if self._export_worker is not None:
            return []
        records = self.store.list(STATUS_PASS)
        if not records:
            self.status_line.setText("无合格素材可导出")
            QMessageBox.information(self, "批量导出", "没有标记为「合格」的素材。")
            return []
        # M4 磁盘预检：估算不足则弹窗中止
        need = self._estimate_export_bytes(records)
        free = shutil.disk_usage(self.store.root).free
        if need > free:
            QMessageBox.critical(
                self, "磁盘空间不足",
                f"预计导出需要约 {need / 1e9:.1f} GB，"
                f"存储盘剩余 {free / 1e9:.1f} GB。\n"
                "请清理磁盘或更换存储根目录后重试，已中止导出。",
            )
            self.status_line.setText("导出中止：磁盘空间不足")
            return []
        out_root = self.store.root / "exports"
        self._export_worker = _ExportWorker(self, records, out_root)
        self._export_worker.progressed.connect(
            lambda done, total: self.status_line.setText(f"导出中 {done}/{total}…")
        )
        self._export_worker.finished_all.connect(self._on_export_done)
        self.btn_export.setEnabled(False)
        self.status_line.setText(f"导出中 0/{len(records)}…")
        self._export_worker.start()
        return []

    def _on_export_done(self, result: tuple) -> None:
        exported, errors, overwritten = result
        if self._export_worker is not None:
            self._export_worker.wait(3000)
            self._export_worker = None
        self.btn_export.setEnabled(True)
        out_root = self.store.root / "exports"
        core_repo = resolve_core_repo(self.settings.core_repo_path)
        lines = [f"已导出 {len(exported)} 段到:\n{out_root}", ""]
        for d in exported:
            data = json.loads((d / "session.json").read_text(encoding="utf-8"))
            builtin = validate_session_builtin(data)
            core = validate_with_core(d / "session.json", core_repo) if core_repo else None
            check = "内置校验通过" if not builtin else f"内置校验失败 {builtin}"
            if core is not None:
                check += "；core 全量校验通过" if not core else f"；core 校验失败 {core}"
            suffix = "（覆盖已有导出）" if d.name in overwritten else ""
            lines.append(f"{d.name}: {check}{suffix}")
        if errors:
            lines += ["", "失败:"] + errors
        QMessageBox.information(self, "批量导出", "\n".join(lines))
        self.status_line.setText(f"导出完成 {len(exported)} 段 → {out_root}")

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
        if self._export_worker is not None:
            self._export_worker.requestInterruption()
            self._export_worker.wait(10000)
            self._export_worker = None
        if self._player is not None:
            self._player.close()
