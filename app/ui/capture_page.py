"""采集页（PRD F2/F4/F5/F6 界面）：预览 + ROI 框选 + 状态面板 + 采集控制。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.detect import State
from app.session import SessionStore
from app.ui.controller import CaptureController
from app.ui.preview import VIEW_LEFT, VIEW_RIGHT, VIEW_SBS, PreviewWidget
from app.ui.settings import AppSettings
from app.ui.theme import mono_font
from app.ui.widgets import (
    CameraIndicator,
    MiniBar,
    SectionHeader,
    StateBanner,
    TelemetryValue,
    block_widget,
)

_SOURCE_UVC_PREFIX = "UVC 设备 "
_SOURCE_FILE = "视频文件回放…"

_VIEW_MODES = [("左目", VIEW_LEFT), ("右目", VIEW_RIGHT), ("双目并排", VIEW_SBS)]


class CapturePage(QWidget):
    """默认页：实时预览（降频 ~30fps）+ 打击区 ROI + 遥测 + 采集控制。"""

    def __init__(
        self,
        settings: AppSettings,
        store: SessionStore,
        controller: CaptureController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self._build_ui()
        self.attach_controller(controller or CaptureController(settings, store))
        self.preview.set_roi(settings.roi_tuple())
        self._refresh_counts()

    # ---- UI 组装 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(1)

        # 顶栏：ASCII 区段头 + 相机指示（终端绿唯一用途）
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(10, 6, 10, 6)
        header = SectionHeader("[ CAPTURE ]")
        self.camera_indicator = CameraIndicator()
        top_layout.addWidget(header)
        top_layout.addStretch(1)
        top_layout.addWidget(self.camera_indicator)
        root.addWidget(block_widget(top))

        body = QHBoxLayout()
        body.setSpacing(1)
        root.addLayout(body, stretch=1)

        # 左：预览（ROI 框选区）
        self.preview = PreviewWidget()
        self.preview.roi_changed.connect(self._on_roi_changed)
        body.addWidget(block_widget(self.preview), stretch=3)

        # 右：状态面板 + 控制
        side = QVBoxLayout()
        side.setSpacing(1)
        body.addLayout(side, stretch=1)

        self.state_banner = StateBanner("IDLE")
        side.addWidget(block_widget(self.state_banner))

        # 遥测计数区（高密度两列）
        metrics = QWidget()
        grid = QHBoxLayout(metrics)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(24)
        col1 = QVBoxLayout()
        col2 = QVBoxLayout()
        grid.addLayout(col1)
        grid.addLayout(col2)
        self.m_total = TelemetryValue("已采集", "0")
        self.m_pass = TelemetryValue("合格", "0")
        self.m_review = TelemetryValue("待审核", "0")
        self.m_fps = TelemetryValue("帧率", "--")
        self.m_ratio = TelemetryValue("就位占比", "--")
        self.m_energy = TelemetryValue("运动能量", "--", accent=True)
        for w in (self.m_total, self.m_fps):
            col1.addWidget(w)
        for w in (self.m_pass, self.m_review):
            col2.addWidget(w)
        col1.addWidget(self.m_ratio)
        col2.addWidget(self.m_energy)
        self.energy_bar = MiniBar(maximum=40.0)
        col2.addWidget(self.energy_bar)
        side.addWidget(block_widget(metrics))

        # 控制区
        controls = QWidget()
        ctl = QVBoxLayout(controls)
        ctl.setContentsMargins(0, 0, 0, 0)
        ctl.setSpacing(8)
        ctl.addWidget(SectionHeader(">>> CONTROL"))

        self.btn_start = QPushButton("开始采集")
        self.btn_start.setObjectName("primary")
        self.btn_start.setFont(mono_font(11, bold=True))
        self.btn_start.clicked.connect(self._on_start_clicked)
        ctl.addWidget(self.btn_start)

        row = QHBoxLayout()
        self.btn_manual_start = QPushButton("手动开始挥棒")
        self.btn_manual_stop = QPushButton("手动结束")
        self.btn_manual_start.clicked.connect(lambda: self.controller.manual_start())
        self.btn_manual_stop.clicked.connect(lambda: self.controller.manual_stop())
        row.addWidget(self.btn_manual_start)
        row.addWidget(self.btn_manual_stop)
        ctl.addLayout(row)

        self.btn_discard = QPushButton("丢弃重拍")
        self.btn_discard.clicked.connect(lambda: self.controller.discard())
        ctl.addWidget(self.btn_discard)

        self.chk_mute = QCheckBox("静音")
        self.chk_mute.setFont(mono_font(10))
        self.chk_mute.toggled.connect(self.controller_muted)
        ctl.addWidget(self.chk_mute)

        src_row = QHBoxLayout()
        src_label = QLabel("相机源")
        src_label.setObjectName("dim")
        src_label.setFont(mono_font(9, letter_spacing=2.0))
        self.combo_source = QComboBox()
        self.combo_source.setFont(mono_font(10))
        for i in range(3):
            self.combo_source.addItem(f"{_SOURCE_UVC_PREFIX}{i}")
        self.combo_source.addItem(_SOURCE_FILE)
        self.combo_source.currentIndexChanged.connect(self._on_source_changed)
        src_row.addWidget(src_label)
        src_row.addWidget(self.combo_source, stretch=1)
        ctl.addLayout(src_row)

        view_row = QHBoxLayout()
        view_label = QLabel("视图")
        view_label.setObjectName("dim")
        view_label.setFont(mono_font(9, letter_spacing=2.0))
        self.combo_view = QComboBox()
        self.combo_view.setFont(mono_font(10))
        for name, _mode in _VIEW_MODES:
            self.combo_view.addItem(name)
        self.combo_view.currentIndexChanged.connect(self._on_view_changed)
        view_row.addWidget(view_label)
        view_row.addWidget(self.combo_view, stretch=1)
        ctl.addLayout(view_row)
        ctl.addStretch(1)
        side.addWidget(block_widget(controls), stretch=1)

        # 状态栏消息
        self.status_line = QLabel("待命")
        self.status_line.setObjectName("dim")
        self.status_line.setFont(mono_font(10, letter_spacing=1.5))
        root.addWidget(block_widget(self.status_line))

    # ---- 控制器接线 ----

    def attach_controller(self, controller: CaptureController) -> None:
        self.controller = controller
        controller.preview_ready.connect(self._on_preview)
        controller.transitioned.connect(self._on_transition)
        controller.telemetry.connect(self._on_telemetry)
        controller.clip_saved.connect(lambda _record: self._refresh_counts())
        controller.error_occurred.connect(self._on_error)
        controller.camera_active.connect(self.camera_indicator.set_connected)

    def controller_muted(self, muted: bool) -> None:
        self.controller.set_muted(muted)

    # ---- 信号槽 ----

    def _on_preview(self, payload) -> None:
        _idx, _ts, left, right = payload
        self.preview.set_frames(left, right)

    def _on_transition(self, transition) -> None:
        state: State = transition.next
        self.state_banner.set_state(state.value, alarm=state is State.ERROR)
        self.status_line.setText(f">>> {transition.prev.value} → {state.value} ({transition.reason})")
        self._refresh_counts()

    def _on_telemetry(self, fps: float, ratio: float, energy: float) -> None:
        self.m_fps.set_value(f"{fps:5.1f}")
        self.m_ratio.set_value(f"{ratio * 100:4.1f}%")
        self.m_energy.set_value(f"{energy:5.2f}")
        self.energy_bar.set_value(energy)

    def _on_error(self, message: str) -> None:
        self.state_banner.set_state("ERROR", alarm=True)
        self.status_line.setText(f">>> ERROR: {message}")

    def _on_roi_changed(self, x: int, y: int, w: int, h: int) -> None:
        self.controller.update_roi((x, y, w, h))
        self.status_line.setText(f">>> ROI 已保存 ({x},{y} {w}x{h})")

    # ---- 控制 ----

    def _on_start_clicked(self) -> None:
        if not self.controller.running:
            if self.controller.start():
                self.btn_start.setText("暂停")
                self.status_line.setText(">>> 采集运行中")
        elif self.controller.paused:
            self.controller.resume()
            self.btn_start.setText("暂停")
            self.status_line.setText(">>> 采集继续")
        else:
            self.controller.pause()
            self.btn_start.setText("继续")
            self.status_line.setText(">>> 采集已暂停（预览保持）")

    def _on_source_changed(self, index: int) -> None:
        text = self.combo_source.currentText()
        if text == _SOURCE_FILE:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择视频文件", str(Path.home()),
                "视频文件 (*.mkv *.mp4 *.avi *.mov)",
            )
            if path:
                self.set_file_source(path)
        else:
            self.settings.camera_index = int(text.removeprefix(_SOURCE_UVC_PREFIX))
            self.settings.save()

    def set_file_source(self, path: str) -> None:
        """切到视频文件回放源（无相机演示模式）。"""
        from app.capture import FileSource

        was_running = self.controller.running
        self.controller.stop()
        self.controller = CaptureController(
            self.settings, self.store, source=FileSource(path, loop=True)
        )
        self.attach_controller(self.controller)
        self.status_line.setText(f">>> 文件回放: {Path(path).name}")
        if was_running:
            self.controller.start()

    def _on_view_changed(self, index: int) -> None:
        self.preview.set_view_mode(_VIEW_MODES[index][1])

    # ---- 数据 ----

    def _refresh_counts(self) -> None:
        counts = self.store.counts()
        self.m_total.set_value(str(counts["total"]))
        self.m_pass.set_value(str(counts["合格"]))
        self.m_review.set_value(str(counts["待复核"]))

    def shutdown(self) -> None:
        self.controller.close()
