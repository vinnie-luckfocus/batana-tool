"""设置页：采集模式、检测阈值、pre/post-roll、语音、存储根目录（PRD F1–F6 参数）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
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

from app.capture.devices import VideoDevice
from app.ui.settings import AppSettings
from app.ui.theme import ui_font
from app.ui.widgets import SectionHeader, card_widget

_FORMATS = ["auto", "mono8", "yuy2", "mjpeg"]
# HBVCAM-W2237-2 实测档位（SBS 合并帧）：120fps / 60fps / 100fps
_RESOLUTIONS = ["1280x400", "2560x720", "1600x600"]


class SettingsPage(QWidget):
    """参数表单：编辑后点「保存设置」落盘 settings.json。"""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._build_ui()
        self._load_values()
        # M5：选 MJPEG 时弹警告（须在 _load_values 之后连接，避免初始化误触发）
        self.combo_format.currentTextChanged.connect(self._on_format_changed)
        # L5：段数/单段大小变更时刷新磁盘占用估算
        self.spin_env_clips.valueChanged.connect(self._refresh_disk_estimate)
        self.spin_env_mb.valueChanged.connect(self._refresh_disk_estimate)
        self._refresh_disk_estimate()

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
        self.spin_trigger.setRange(0.1, 255.0)
        self.spin_trigger.setDecimals(1)
        form.addRow(label("能量触发阈值"), self.spin_trigger)
        self.spin_release = QDoubleSpinBox()
        self.spin_release.setRange(0.1, 255.0)
        self.spin_release.setDecimals(1)
        form.addRow(label("能量回落阈值"), self.spin_release)
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
        form.addRow(label("预录缓冲（秒）"), self.spin_buffer)

        form.addRow(SectionHeader("语音"))
        self.chk_voice = QCheckBox("启用语音引导")
        form.addRow(label("语音开关"), self.chk_voice)
        self.spin_rate = QSpinBox()
        self.spin_rate.setRange(80, 400)
        self.spin_rate.setSingleStep(10)
        form.addRow(label("语速（词/分）"), self.spin_rate)

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
        self.edit_pose_model.setPlaceholderText("留空 = StubPoseEstimator")
        form.addRow(label("POSE 模型文件"), self.edit_pose_model)

        self.edit_core_repo = QLineEdit()
        self.edit_core_repo.setPlaceholderText("留空 = 自动探测常见位置")
        form.addRow(label("CORE 仓路径"), self.edit_core_repo)

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
        self.spin_trigger.setValue(s.energy_trigger)
        self.spin_release.setValue(s.energy_release)
        self.spin_pre.setValue(s.pre_roll_seconds)
        self.spin_post.setValue(s.post_roll_seconds)
        self.spin_buffer.setValue(s.buffer_seconds)
        self.chk_voice.setChecked(s.voice_enabled)
        self.spin_rate.setValue(s.voice_rate)
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
        w, h = self.combo_resolution.currentText().split("x")
        s.capture_width, s.capture_height = int(w), int(h)
        s.capture_fps = float(self.spin_fps.value())
        s.pixel_format = self.combo_format.currentText()
        cam = self.combo_camera.currentData()
        if isinstance(cam, VideoDevice):
            s.camera_index = cam.index
            s.camera_name = cam.name
        s.presence_ratio = float(self.spin_presence.value())
        s.energy_trigger = float(self.spin_trigger.value())
        s.energy_release = float(self.spin_release.value())
        s.pre_roll_seconds = float(self.spin_pre.value())
        s.post_roll_seconds = float(self.spin_post.value())
        s.buffer_seconds = float(self.spin_buffer.value())
        s.voice_enabled = self.chk_voice.isChecked()
        s.voice_rate = int(self.spin_rate.value())
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
