"""设置页：采集模式、检测阈值、pre/post-roll、语音、存储根目录（PRD F1–F6 参数）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui.settings import AppSettings
from app.ui.theme import mono_font
from app.ui.widgets import SectionHeader, block_widget

_FORMATS = ["auto", "mono8", "yuy2", "mjpeg"]
_RESOLUTIONS = ["2560x800", "1280x400", "640x200"]


class SettingsPage(QWidget):
    """参数表单：编辑后点「保存设置」落盘 settings.json。"""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(1)

        top = block_widget(SectionHeader("[ SETTINGS ]"))
        top.layout().setContentsMargins(10, 6, 10, 6)
        root.addWidget(top)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        def label(text: str) -> QLabel:
            w = QLabel(text)
            w.setObjectName("dim")
            w.setFont(mono_font(10, letter_spacing=2.0))
            return w

        form.addRow(SectionHeader(">>> 采集模式"))
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
        self.spin_camera = QSpinBox()
        self.spin_camera.setRange(0, 8)
        form.addRow(label("UVC 设备序号"), self.spin_camera)

        form.addRow(SectionHeader(">>> 检测阈值"))
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

        form.addRow(SectionHeader(">>> 语音"))
        self.chk_voice = QCheckBox("启用语音引导")
        self.chk_voice.setFont(mono_font(10))
        form.addRow(label("语音开关"), self.chk_voice)
        self.spin_rate = QSpinBox()
        self.spin_rate.setRange(80, 400)
        self.spin_rate.setSingleStep(10)
        form.addRow(label("语速（词/分）"), self.spin_rate)

        form.addRow(SectionHeader(">>> 存储"))
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

        root.addWidget(block_widget(form_host), stretch=1)

        bottom = QWidget()
        bt = QHBoxLayout(bottom)
        bt.setContentsMargins(0, 0, 0, 0)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        self.label_saved = QLabel("")
        self.label_saved.setObjectName("dim")
        self.label_saved.setFont(mono_font(10, letter_spacing=1.5))
        bt.addWidget(self.btn_save)
        bt.addWidget(self.label_saved, stretch=1)
        root.addWidget(block_widget(bottom))

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
        self.spin_camera.setValue(s.camera_index)
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
        s.camera_index = int(self.spin_camera.value())
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
        path = s.save()
        self.label_saved.setText(f">>> 已保存 {Path(path).name}")
