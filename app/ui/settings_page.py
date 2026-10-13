"""设置页：采集模式、检测阈值、pre/post-roll、语音、存储根目录（PRD F1–F6 参数）。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.calib.frames import extract_frame_pairs
from app.calib.nominal import nominal_calibration
from app.calib.runner import run_core_calibration
from app.calib.store import (
    DEFAULT_CALIBRATION_PATH,
    calibration_status,
    save_calibration,
)
from app.capture.devices import VideoDevice
from app.session.export import resolve_core_repo
from app.ui.settings import AppSettings
from app.ui.theme import ui_font
from app.ui.widgets import SectionHeader, card_widget

_FORMATS = ["auto", "mono8", "yuy2", "mjpeg"]
# HBVCAM-W2237-2 实测档位（SBS 合并帧）：120fps / 60fps / 100fps
_RESOLUTIONS = ["1280x400", "2560x720", "1600x600"]


class _CalibWorker(QThread):
    """标定线程：抽帧到临时目录 → core 标定 → 清理临时目录（不阻塞 UI）。"""

    finished_ok = Signal(object)  # 标定结果 dict
    failed = Signal(str)

    def __init__(self, clip_dir: Path, core_repo: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip_dir = clip_dir
        self._core_repo = core_repo

    def run(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="batana-calib-"))
        try:
            extract_frame_pairs(self._clip_dir, tmp)
            data = run_core_calibration(tmp, self._core_repo, tmp / "calib.json")
            self.finished_ok.emit(data)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SettingsPage(QWidget):
    """参数表单：编辑后点「保存设置」落盘 settings.json。"""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._calib_worker: _CalibWorker | None = None
        self._build_ui()
        self._load_values()
        # M5：选 MJPEG 时弹警告（须在 _load_values 之后连接，避免初始化误触发）
        self.combo_format.currentTextChanged.connect(self._on_format_changed)
        # L5：段数/单段大小变更时刷新磁盘占用估算
        self.spin_env_clips.valueChanged.connect(self._refresh_disk_estimate)
        self.spin_env_mb.valueChanged.connect(self._refresh_disk_estimate)
        self._refresh_disk_estimate()
        self._refresh_calib_status()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        form.setHorizontalSpacing(16)
        # 标签列等宽右对齐，控件列弹性拉伸（8pt 网格统一）
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        def label(text: str) -> QLabel:
            w = QLabel(text)
            w.setObjectName("dim")
            w.setFont(ui_font(12))
            return w

        form.addRow(SectionHeader("采集模式"))
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems(_RESOLUTIONS)
        form.addRow(label("分辨率"), self.combo_resolution)
        self.spin_fps = QDoubleSpinBox()
        self.spin_fps.setRange(1, 240)
        self.spin_fps.setDecimals(0)
        form.addRow(label("帧率 FPS"), self.spin_fps)
        self.combo_format = QComboBox()
        self.combo_format.addItems(_FORMATS)
        form.addRow(label("像素格式"), self.combo_format)
        cam_row = QHBoxLayout()
        self.combo_camera = QComboBox()
        self.combo_camera.setMinimumWidth(260)
        self.btn_refresh_cam = QPushButton("刷新")
        self.btn_refresh_cam.setFixedWidth(64)
        self.btn_refresh_cam.clicked.connect(self._refresh_cameras)
        cam_row.addWidget(self.combo_camera, stretch=1)
        cam_row.addWidget(self.btn_refresh_cam)
        cam_host = QWidget()
        cam_host.setLayout(cam_row)
        cam_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(label("相机设备"), cam_host)
        self._refresh_cameras()

        form.addRow(SectionHeader("检测阈值"))
        self.spin_presence = QDoubleSpinBox()
        self.spin_presence.setRange(0.001, 1.0)
        self.spin_presence.setDecimals(3)
        self.spin_presence.setSingleStep(0.005)
        form.addRow(label("就位占比阈值"), self.spin_presence)
        self.spin_trigger = QDoubleSpinBox()
        self.spin_trigger.setRange(0.1, 50.0)
        self.spin_trigger.setDecimals(1)
        self.spin_trigger.setSuffix(" %")
        self.spin_trigger.setToolTip("显著运动像素占 ROI 比例超过此值判定挥棒开始")
        form.addRow(label("挥棒触发（运动占比）"), self.spin_trigger)
        self.spin_release = QDoubleSpinBox()
        self.spin_release.setRange(0.1, 50.0)
        self.spin_release.setDecimals(1)
        self.spin_release.setSuffix(" %")
        self.spin_release.setToolTip("运动占比低于此值并持续 POST-ROLL 判定挥棒结束")
        form.addRow(label("挥棒回落（运动占比）"), self.spin_release)
        self.spin_pre = QDoubleSpinBox()
        self.spin_pre.setRange(0.0, 10.0)
        self.spin_pre.setDecimals(1)
        self.spin_pre.setSingleStep(0.5)
        form.addRow(label("PRE-ROLL（秒）"), self.spin_pre)
        self.spin_post = QDoubleSpinBox()
        self.spin_post.setRange(0.1, 10.0)
        self.spin_post.setDecimals(1)
        self.spin_post.setSingleStep(0.5)
        form.addRow(label("POST-ROLL（秒）"), self.spin_post)
        self.spin_buffer = QDoubleSpinBox()
        self.spin_buffer.setRange(1.0, 30.0)
        self.spin_buffer.setDecimals(1)
        self.spin_buffer.setToolTip("运行时自动抬高到 PRE-ROLL + 最长挥棒 + 1 秒以上")
        form.addRow(label("预录缓冲（秒）"), self.spin_buffer)
        self.spin_max_swing = QDoubleSpinBox()
        self.spin_max_swing.setRange(1.0, 10.0)
        self.spin_max_swing.setDecimals(1)
        self.spin_max_swing.setSingleStep(0.5)
        self.spin_max_swing.setToolTip("运动长时间不回落时强制收尾，防止缓冲溢出丢失挥棒起点")
        form.addRow(label("最长挥棒（秒）"), self.spin_max_swing)
        self.spin_settle = QDoubleSpinBox()
        self.spin_settle.setRange(0.0, 10.0)
        self.spin_settle.setDecimals(1)
        self.spin_settle.setSingleStep(0.5)
        self.spin_settle.setToolTip("素材保存完成后，等待该时长再开始下一次倒计时（留复位时间）")
        form.addRow(label("保存后沉淀（秒）"), self.spin_settle)

        form.addRow(SectionHeader("存储"))
        dir_row = QHBoxLayout()
        self.edit_root = QLineEdit()
        self.btn_browse = QPushButton("…")
        self.btn_browse.setFixedWidth(40)
        self.btn_browse.clicked.connect(self._browse_root)
        dir_row.addWidget(self.edit_root, stretch=1)
        dir_row.addWidget(self.btn_browse)
        dir_host = QWidget()
        dir_host.setLayout(dir_row)
        dir_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(label("存储根目录"), dir_host)

        self.edit_pose_model = QLineEdit()
        self.edit_pose_model.setPlaceholderText("留空 = 自动探测 batana-core/models/files/*.task")
        form.addRow(label("POSE 模型文件"), self.edit_pose_model)

        self.edit_core_repo = QLineEdit()
        self.edit_core_repo.setPlaceholderText("留空 = 自动探测常见位置")
        form.addRow(label("CORE 仓路径"), self.edit_core_repo)

        form.addRow(SectionHeader("相机标定"))
        self.label_calib_status = QLabel("")
        self.label_calib_status.setFont(ui_font(12))
        form.addRow(label("当前标定"), self.label_calib_status)
        calib_row = QHBoxLayout()
        self.btn_calibrate = QPushButton("从片段抽帧并标定…")
        self.btn_calibrate.setToolTip("选一段素材，均匀抽帧后调 batana-core ChArUco 双目标定")
        self.btn_calibrate.clicked.connect(self._calibrate_from_clip)
        self.btn_nominal_calib = QPushButton("使用名义标定（仅冒烟）")
        self.btn_nominal_calib.setToolTip(
            "按模组规格估算内参/基线 120mm，仅供链路冒烟，不能用于精度结论")
        self.btn_nominal_calib.clicked.connect(self._use_nominal_calib)
        self.btn_clear_calib = QPushButton("清除标定")
        self.btn_clear_calib.clicked.connect(self._clear_calib)
        calib_row.addWidget(self.btn_calibrate)
        calib_row.addWidget(self.btn_nominal_calib)
        calib_row.addWidget(self.btn_clear_calib)
        calib_host = QWidget()
        calib_host.setLayout(calib_row)
        calib_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(label("标定操作"), calib_host)

        form.addRow(SectionHeader("环境检查"))
        self.spin_env_bright_fail = QDoubleSpinBox()
        self.spin_env_bright_fail.setRange(0, 255)
        self.spin_env_bright_fail.setDecimals(0)
        form.addRow(label("亮度不合格下限"), self.spin_env_bright_fail)
        self.spin_env_bright_warn = QDoubleSpinBox()
        self.spin_env_bright_warn.setRange(0, 255)
        self.spin_env_bright_warn.setDecimals(0)
        form.addRow(label("亮度警告下限"), self.spin_env_bright_warn)
        self.spin_env_flicker_warn = QDoubleSpinBox()
        self.spin_env_flicker_warn.setRange(0.1, 50.0)
        self.spin_env_flicker_warn.setDecimals(1)
        self.spin_env_flicker_warn.setSuffix(" %")
        form.addRow(label("频闪警告阈值"), self.spin_env_flicker_warn)
        self.spin_env_flicker_fail = QDoubleSpinBox()
        self.spin_env_flicker_fail.setRange(0.5, 100.0)
        self.spin_env_flicker_fail.setDecimals(1)
        self.spin_env_flicker_fail.setSuffix(" %")
        form.addRow(label("频闪不合格阈值"), self.spin_env_flicker_fail)
        self.spin_env_sharpness = QDoubleSpinBox()
        self.spin_env_sharpness.setRange(1.0, 1000.0)
        self.spin_env_sharpness.setDecimals(0)
        form.addRow(label("清晰度阈值"), self.spin_env_sharpness)
        self.spin_env_level_warn = QDoubleSpinBox()
        self.spin_env_level_warn.setRange(0.1, 45.0)
        self.spin_env_level_warn.setDecimals(1)
        self.spin_env_level_warn.setSuffix(" °")
        form.addRow(label("倾角警告阈值"), self.spin_env_level_warn)
        self.spin_env_level_fail = QDoubleSpinBox()
        self.spin_env_level_fail.setRange(0.5, 90.0)
        self.spin_env_level_fail.setDecimals(1)
        self.spin_env_level_fail.setSuffix(" °")
        form.addRow(label("倾角不合格阈值"), self.spin_env_level_fail)
        self.spin_env_clips = QSpinBox()
        self.spin_env_clips.setRange(1, 5000)
        form.addRow(label("计划采集段数"), self.spin_env_clips)
        self.spin_env_mb = QDoubleSpinBox()
        self.spin_env_mb.setRange(1.0, 100000.0)
        self.spin_env_mb.setDecimals(0)
        self.spin_env_mb.setSuffix(" MB")
        form.addRow(label("单段估算大小"), self.spin_env_mb)
        # L5 磁盘占用静态估算（计划段数 × 单段大小）
        self.label_disk_est = QLabel("")
        self.label_disk_est.setObjectName("dim")
        self.label_disk_est.setFont(ui_font(12))
        form.addRow(label("预计磁盘占用"), self.label_disk_est)

        # 表单超高时可滚动（HIG：设置内容垂直滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(card_widget(form_host))  # 滚动区内不挂阴影（会被视口裁剪）
        root.addWidget(scroll, stretch=1)

        bottom = QWidget()
        bt = QHBoxLayout(bottom)
        bt.setContentsMargins(0, 0, 0, 0)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        self.label_saved = QLabel("")
        self.label_saved.setObjectName("dim")
        self.label_saved.setFont(ui_font(12))
        bt.addWidget(self.btn_save)
        bt.addWidget(self.label_saved, stretch=1)
        root.addWidget(card_widget(bottom))

    def _refresh_cameras(self) -> None:
        """枚举 avfoundation 视频设备填充下拉框；优先保留当前选择。"""
        from app.capture import list_video_devices, pick_default

        current = self.combo_camera.currentData()
        prev_name = (
            current.name if isinstance(current, VideoDevice) else self.settings.camera_name
        )
        devices = list_video_devices()
        self.combo_camera.clear()
        if not devices:
            self.combo_camera.addItem("未检测到相机（点「刷新」重试）", None)
            return
        for d in devices:
            tag = " · 双目模组" if d.is_stereo_module else ""
            self.combo_camera.addItem(f"{d.name}（#{d.index}）{tag}", d)
        idx = next((i for i, d in enumerate(devices) if d.name == prev_name), -1)
        if idx < 0:
            idx = devices.index(pick_default(devices))
        self.combo_camera.setCurrentIndex(idx)

    def _load_values(self) -> None:
        s = self.settings
        res = f"{s.capture_width}x{s.capture_height}"
        if res not in _RESOLUTIONS:
            self.combo_resolution.addItem(res)
        self.combo_resolution.setCurrentText(res)
        self.spin_fps.setValue(s.capture_fps)
        self.combo_format.setCurrentText(
            s.pixel_format if s.pixel_format in _FORMATS else "auto"
        )
        self.spin_presence.setValue(s.presence_ratio)
        self.spin_trigger.setValue(s.motion_trigger_pct)
        self.spin_release.setValue(s.motion_release_pct)
        self.spin_pre.setValue(s.pre_roll_seconds)
        self.spin_post.setValue(s.post_roll_seconds)
        self.spin_buffer.setValue(s.buffer_seconds)
        self.spin_max_swing.setValue(s.max_swing_seconds)
        self.spin_settle.setValue(s.post_save_settle_seconds)
        self.edit_root.setText(s.storage_root)
        self.edit_pose_model.setText(s.pose_model_path)
        self.spin_env_bright_fail.setValue(s.env_brightness_fail)
        self.spin_env_bright_warn.setValue(s.env_brightness_warn)
        self.spin_env_flicker_warn.setValue(s.env_flicker_warn_pct)
        self.spin_env_flicker_fail.setValue(s.env_flicker_fail_pct)
        self.spin_env_sharpness.setValue(s.env_sharpness_warn)
        self.spin_env_level_warn.setValue(s.env_level_warn_deg)
        self.spin_env_level_fail.setValue(s.env_level_fail_deg)
        self.spin_env_clips.setValue(s.env_planned_clips)
        self.spin_env_mb.setValue(s.env_est_mb_per_clip)
        self.edit_core_repo.setText(s.core_repo_path)

    def _on_format_changed(self, text: str) -> None:
        """M5 防呆：MJPEG 有损压缩仅供冒烟，正式素材应选无压缩。"""
        if text == "mjpeg":
            QMessageBox.warning(
                self, "像素格式警告",
                "MJPEG 为有损压缩，仅供冒烟测试；\n正式素材请使用无压缩格式（auto / mono8 / yuy2）。",
            )

    def _refresh_disk_estimate(self) -> None:
        """L5：磁盘占用静态估算 = 计划段数 × 单段估算大小。"""
        total_mb = self.spin_env_clips.value() * self.spin_env_mb.value()
        if total_mb >= 1024:
            self.label_disk_est.setText(f"{total_mb / 1024:.1f} GB")
        else:
            self.label_disk_est.setText(f"{total_mb:.0f} MB")

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择存储根目录", self.edit_root.text())
        if path:
            self.edit_root.setText(path)

    def save(self) -> None:
        s = self.settings
        old_res = (s.capture_width, s.capture_height)
        w, h = self.combo_resolution.currentText().split("x")
        s.capture_width, s.capture_height = int(w), int(h)
        # 分辨率变更时 ROI（左目像素坐标）按比例缩放，避免框选错位
        if (s.capture_width, s.capture_height) != old_res and s.roi is not None:
            fx = s.capture_width / old_res[0]
            fy = s.capture_height / old_res[1]
            s.roi = [
                int(s.roi[0] * fx), int(s.roi[1] * fy),
                int(s.roi[2] * fx), int(s.roi[3] * fy),
            ]
        s.capture_fps = float(self.spin_fps.value())
        s.pixel_format = self.combo_format.currentText()
        cam = self.combo_camera.currentData()
        if isinstance(cam, VideoDevice):
            s.camera_index = cam.index
            s.camera_name = cam.name
        s.presence_ratio = float(self.spin_presence.value())
        s.motion_trigger_pct = float(self.spin_trigger.value())
        s.motion_release_pct = float(self.spin_release.value())
        s.pre_roll_seconds = float(self.spin_pre.value())
        s.post_roll_seconds = float(self.spin_post.value())
        s.buffer_seconds = float(self.spin_buffer.value())
        s.max_swing_seconds = float(self.spin_max_swing.value())
        s.post_save_settle_seconds = float(self.spin_settle.value())
        s.storage_root = self.edit_root.text().strip() or s.storage_root
        s.pose_model_path = self.edit_pose_model.text().strip()
        s.core_repo_path = self.edit_core_repo.text().strip()
        s.env_brightness_fail = float(self.spin_env_bright_fail.value())
        s.env_brightness_warn = float(self.spin_env_bright_warn.value())
        s.env_flicker_warn_pct = float(self.spin_env_flicker_warn.value())
        s.env_flicker_fail_pct = float(self.spin_env_flicker_fail.value())
        s.env_sharpness_warn = float(self.spin_env_sharpness.value())
        s.env_level_warn_deg = float(self.spin_env_level_warn.value())
        s.env_level_fail_deg = float(self.spin_env_level_fail.value())
        s.env_planned_clips = int(self.spin_env_clips.value())
        s.env_est_mb_per_clip = float(self.spin_env_mb.value())
        path = s.save()
        self.label_saved.setText(f"已保存 {Path(path).name}")

    # ---- 相机标定 ----

    def _refresh_calib_status(self) -> None:
        """刷新标定状态行与按钮可用性（core 仓不可用时禁用标定按钮）。"""
        self.label_calib_status.setText(calibration_status())
        core = resolve_core_repo(self.settings.core_repo_path)
        busy = self._calib_worker is not None
        self.btn_calibrate.setEnabled(core is not None and not busy)
        if core is None:
            self.btn_calibrate.setToolTip("batana-core 仓不可用（在上方 CORE 仓路径配置后保存并重开设置页）")
        else:
            self.btn_calibrate.setToolTip("选一段素材，均匀抽帧后调 batana-core ChArUco 双目标定")
        self.btn_nominal_calib.setEnabled(not busy)
        self.btn_clear_calib.setEnabled(not busy)

    def _calibrate_from_clip(self) -> None:
        """从素材片段抽帧并标定（后台线程），成功则落盘并刷新状态。"""
        if self._calib_worker is not None:
            return
        core = resolve_core_repo(self.settings.core_repo_path)
        if core is None:
            QMessageBox.warning(self, "相机标定", "batana-core 仓不可用，无法标定。")
            return
        start_dir = str(Path(self.edit_root.text().strip() or self.settings.storage_root) / "sessions")
        clip_dir = QFileDialog.getExistingDirectory(self, "选择素材片段目录", start_dir)
        if not clip_dir:
            return
        self._calib_worker = _CalibWorker(Path(clip_dir), core, parent=self)
        self._calib_worker.finished_ok.connect(self._on_calib_done)
        self._calib_worker.failed.connect(self._on_calib_failed)
        self._calib_worker.finished.connect(self._on_calib_worker_finished)
        self._refresh_calib_status()
        self.label_calib_status.setText("标定中…（抽帧 + core 双目标定）")
        self._calib_worker.start()

    def _on_calib_done(self, data: dict) -> None:
        save_calibration(data)
        self._refresh_calib_status()
        report = data.get("report") or {}
        rms = (report.get("reprojection_error_px") or {})
        QMessageBox.information(
            self, "相机标定",
            f"标定完成并已存档：{DEFAULT_CALIBRATION_PATH}\n\n"
            f"基线 {data['stereo']['baseline_mm']:.2f} mm\n"
            f"重投影误差 px：左目 {rms.get('left', 0):.3f} / "
            f"右目 {rms.get('right', 0):.3f} / 立体对 {rms.get('stereo', 0):.3f}\n"
            f"使用帧数 {report.get('views_used', '?')}",
        )

    def _on_calib_failed(self, message: str) -> None:
        self._refresh_calib_status()
        QMessageBox.warning(self, "相机标定失败", message)

    def _on_calib_worker_finished(self) -> None:
        self._calib_worker = None
        self._refresh_calib_status()

    def _use_nominal_calib(self) -> None:
        """按当前采集分辨率的单目尺寸生成名义标定并存档（仅冒烟）。"""
        try:
            w, h = self.combo_resolution.currentText().split("x")
            eye_w, eye_h = int(w) // 2, int(h)
        except ValueError:
            eye_w, eye_h = self.settings.capture_width // 2, self.settings.capture_height
        data = nominal_calibration(eye_w, eye_h)
        save_calibration(data)
        self._refresh_calib_status()
        QMessageBox.information(
            self, "名义标定",
            f"已按单目 {eye_w}x{eye_h}、基线 120mm 生成名义标定并存档。\n"
            "这是模组规格估算值，仅供链路冒烟，不能用于精度结论。",
        )

    def _clear_calib(self) -> None:
        DEFAULT_CALIBRATION_PATH.unlink(missing_ok=True)
        self._refresh_calib_status()
