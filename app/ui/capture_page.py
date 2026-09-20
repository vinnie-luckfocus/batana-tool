"""采集页（PRD F2/F4/F5/F6 界面）：预览 + ROI 框选 + 状态面板 + 采集控制。"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.capture import FileSource, UvcSource
from app.detect import State
from app.envcheck import EnvCheckSettings, EnvironmentChecker
from app.session import SessionStore
from app.ui.controller import CaptureController
from app.ui.envcheck_dialog import EnvCheckDialog
from app.ui.preview import VIEW_LEFT, VIEW_RIGHT, VIEW_SBS, PreviewWidget
from app.ui.settings import AppSettings
from app.ui.theme import ui_font
from app.ui.widgets import (
    CameraIndicator,
    MiniBar,
    NotificationBanner,
    SectionHeader,
    StateBanner,
    TelemetryValue,
    card_widget,
)
from app.voice import PROMPT_NO_SWING, error_prompt

_SOURCE_UVC_PREFIX = "UVC 设备 "
_SOURCE_FILE = "视频文件回放…"

_VIEW_MODES = [("左目", VIEW_LEFT), ("右目", VIEW_RIGHT), ("双目并排", VIEW_SBS)]

ROI_HINT_TEXT = "请先在画面上框选打击区"
NO_SWING_TIMEOUT_MS = 15000  # M3：ARMED 持续 15s 无挥棒的语音提醒间隔


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
        self._file_path: str | None = None  # 文件回放模式下的源路径（环境检查复用）
        self._build_ui()
        self.attach_controller(controller or CaptureController(settings, store))
        self.preview.set_roi(settings.roi_tuple())
        self._refresh_counts()
        self._refresh_roi_hint()
        self.energy_bar.set_threshold(settings.energy_trigger)
        # H1 视觉倒计时：跟随 READY 态起始时间在横幅上倒数（不改核心层语义）
        self._ready_since: float | None = None
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        # M3：ARMED 持续无挥棒 → 语音提示（UI 层定时器，不改状态机）
        self._no_swing_interval_ms = NO_SWING_TIMEOUT_MS
        self._no_swing_timer = QTimer(self)
        self._no_swing_timer.timeout.connect(self._on_no_swing)
        # F11 持续监测：采集中每 5s 用最近 ~1s 帧评估亮度/频闪（纯视觉提示，不打扰语音）
        self._env_timer = QTimer(self)
        self._env_timer.setInterval(5000)
        self._env_timer.timeout.connect(self._monitor_env)
        self._env_timer.start()
        # L1 快捷键：空格 = 手动开始/结束挥棒，D = 丢弃重拍
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.manual_toggle)
        QShortcut(QKeySequence(Qt.Key.Key_D), self, activated=self.discard_clip)

    # ---- UI 组装 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # 顶栏：相机连接状态（右侧）
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addStretch(1)
        self.camera_indicator = CameraIndicator()
        top_layout.addWidget(self.camera_indicator)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, stretch=1)

        # 左：预览（ROI 框选区）+ 未框选常显提示（M5 首次引导）
        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.roi_hint = QLabel(ROI_HINT_TEXT)
        self.roi_hint.setFont(ui_font(12))
        self.roi_hint.setObjectName("warning")  # 颜色走全局 QSS，随外观切换
        self.roi_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_layout.addWidget(self.roi_hint)
        self.preview = PreviewWidget()
        self.preview.roi_changed.connect(self._on_roi_changed)
        left_layout.addWidget(self.preview, stretch=1)
        body.addWidget(card_widget(left_col), stretch=3)

        # 右：状态面板 + 控制
        side = QVBoxLayout()
        side.setSpacing(12)
        body.addLayout(side, stretch=1)

        self.state_banner = StateBanner("IDLE")
        side.addWidget(self.state_banner)

        # F11 环境异常通知横幅（持续监测结果；无异常时隐藏）
        self.env_banner = NotificationBanner()
        side.addWidget(self.env_banner)

        # 遥测计数区（两列等宽网格，MiniBar 拉满整行）
        metrics = QWidget()
        grid = QGridLayout(metrics)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.m_total = TelemetryValue("已采集", "0")
        self.m_pass = TelemetryValue("合格", "0")
        self.m_review = TelemetryValue("待审核", "0")
        self.m_fps = TelemetryValue("帧率", "--")
        self.m_ratio = TelemetryValue("就位占比", "--")
        self.m_energy = TelemetryValue("运动能量", "--", accent=True)
        grid.addWidget(self.m_total, 0, 0)
        grid.addWidget(self.m_pass, 0, 1)
        grid.addWidget(self.m_fps, 1, 0)
        grid.addWidget(self.m_review, 1, 1)
        grid.addWidget(self.m_ratio, 2, 0)
        grid.addWidget(self.m_energy, 2, 1)
        self.energy_bar = MiniBar(maximum=40.0)
        grid.addWidget(self.energy_bar, 3, 0, 1, 2)  # 拉满两列
        side.addWidget(card_widget(metrics, shadow=True))

        # 控制区
        controls = QWidget()
        ctl = QVBoxLayout(controls)
        ctl.setContentsMargins(0, 0, 0, 0)
        ctl.setSpacing(10)
        ctl.addWidget(SectionHeader("采集控制"))

        self.btn_start = QPushButton("开始采集")
        self.btn_start.setObjectName("primary")
        self.btn_start.setFont(ui_font(13, bold=True))
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self._on_start_clicked)
        ctl.addWidget(self.btn_start)

        row = QHBoxLayout()
        row.setSpacing(24)  # L1：手动开始/结束拉开间距防误触
        self.btn_manual_start = QPushButton("手动开始挥棒")
        self.btn_manual_start.setToolTip("快捷键：空格")
        self.btn_manual_stop = QPushButton("手动结束")
        self.btn_manual_stop.setToolTip("快捷键：空格")
        self.btn_manual_start.clicked.connect(lambda: self.controller.manual_start())
        self.btn_manual_stop.clicked.connect(lambda: self.controller.manual_stop())
        row.addWidget(self.btn_manual_start)
        row.addWidget(self.btn_manual_stop)
        ctl.addLayout(row)

        ctl.addSpacing(8)  # L1：丢弃重拍与手动开始拉开距离防误触
        self.btn_discard = QPushButton("丢弃重拍")
        self.btn_discard.setToolTip("快捷键：D")
        self.btn_discard.clicked.connect(lambda: self.controller.discard())
        ctl.addWidget(self.btn_discard)

        self.btn_envcheck = QPushButton("环境体检")
        self.btn_envcheck.setToolTip("采集前 10 秒环境合规检查（光照/频闪/构图等 8 项）")
        self.btn_envcheck.clicked.connect(self._on_envcheck_clicked)
        ctl.addWidget(self.btn_envcheck)

        self.chk_mute = QCheckBox("静音")
        self.chk_mute.toggled.connect(self.controller_muted)
        ctl.addWidget(self.chk_mute)

        src_row = QHBoxLayout()
        src_label = QLabel("相机源")
        src_label.setObjectName("dim")
        self.combo_source = QComboBox()
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
        self.combo_view = QComboBox()
        for name, _mode in _VIEW_MODES:
            self.combo_view.addItem(name)
        self.combo_view.currentIndexChanged.connect(self._on_view_changed)
        view_row.addWidget(view_label)
        view_row.addWidget(self.combo_view, stretch=1)
        ctl.addLayout(view_row)
        ctl.addStretch(1)
        side.addWidget(card_widget(controls, shadow=True), stretch=1)

        # 状态栏消息
        self.status_line = QLabel("待命")
        self.status_line.setObjectName("dim")
        self.status_line.setFont(ui_font(12))
        root.addWidget(self.status_line)

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
        # M5 首次引导：检测器懒建的默认 ROI 同步回显到预览（不写入设置）
        if self.preview.roi() is None and self.controller.detector_roi is not None:
            self.preview.set_roi(self.controller.detector_roi)

    def _on_transition(self, transition) -> None:
        state: State = transition.next
        self.state_banner.set_state(state.value, alarm=state is State.ERROR)
        self.status_line.setText(f"{transition.prev.value} → {state.value}（{transition.reason}）")
        self._refresh_counts()
        if state is State.READY:
            # H1：进入 READY 开始视觉倒计时（含 SAVING→READY 的循环重启）
            self._ready_since = time.monotonic()
            self._tick_countdown()
            self._countdown_timer.start()
        else:
            self._countdown_timer.stop()
            self._ready_since = None
        if state is State.ARMED:
            self._no_swing_timer.setInterval(self._no_swing_interval_ms)
            self._no_swing_timer.start()
        else:
            self._no_swing_timer.stop()

    def _tick_countdown(self) -> None:
        """READY 倒计时大号数字（3/2/1）；倒计时结束交给状态机切 ARMED。"""
        if self._ready_since is None:
            return
        remaining = self.settings.countdown_seconds - (time.monotonic() - self._ready_since)
        n = math.ceil(remaining)
        self.state_banner.set_countdown(n if 1 <= n <= 9 else None)

    def _on_no_swing(self) -> None:
        """M3：ARMED 超时未检测到挥棒 → 语音提示（可重复直到离开 ARMED）。"""
        self.controller.say(PROMPT_NO_SWING, priority=1)

    def manual_toggle(self) -> None:
        """L1 空格：ARMED 手动开始挥棒 / SWING 手动结束。"""
        sm = self.controller.state_machine
        if sm is None:
            return
        if sm.state is State.SWING:
            self.controller.manual_stop()
        else:
            self.controller.manual_start()

    def discard_clip(self) -> None:
        """L1 D 键：丢弃重拍当前进行中片段。"""
        self.controller.discard()

    def _on_telemetry(self, fps: float, ratio: float, energy: float) -> None:
        self.m_fps.set_value(f"{fps:5.1f}")
        self.m_ratio.set_value(f"{ratio * 100:4.1f}%")
        self.m_energy.set_value(f"{energy:5.2f}")
        self.energy_bar.set_value(energy)

    def _on_error(self, message: str) -> None:
        """H5：异常横幅 + 状态栏分类文案（相机断开 / 存储失败 / 通用异常）。"""
        self.state_banner.set_state("ERROR", alarm=True)
        self.status_line.setText(f"{error_prompt(message)}（{message}）")

    def _on_roi_changed(self, x: int, y: int, w: int, h: int) -> None:
        self.controller.update_roi((x, y, w, h))
        self._refresh_roi_hint()
        self.status_line.setText(f"ROI 已保存 ({x},{y} {w}x{h})")

    def _refresh_roi_hint(self) -> None:
        """M5：未框选 ROI 时常显引导提示，框选后隐藏。"""
        self.roi_hint.setVisible(self.settings.roi_tuple() is None)

    # ---- F11 环境体检 ----

    def _on_envcheck_clicked(self) -> None:
        """打开环境体检对话框：采集中则先暂停，结束后恢复原采集。"""
        was_running = self.controller.running
        if was_running:
            self.controller.stop()
            self.btn_start.setText("开始采集")
            self.status_line.setText("环境检查中，采集已暂停")
        try:
            if self._file_path:
                source = FileSource(self._file_path)
            else:
                s = self.settings
                source = UvcSource(
                    device_index=s.camera_index,
                    width=s.capture_width, height=s.capture_height,
                    fps=s.capture_fps, pixel_format=s.pixel_format,
                )
        except Exception as e:
            self.status_line.setText(f"错误：{e}")
            if was_running:
                self._resume_capture()
            return
        checker = EnvironmentChecker(
            source,
            roi=self.settings.roi_tuple(),
            settings=EnvCheckSettings.from_app_settings(self.settings),
            report_root=self.settings.storage_root,
        )
        dialog = EnvCheckDialog(checker, duration_s=10.0, parent=self)
        dialog.exec()
        source.close()
        if was_running:
            self._resume_capture()

    def _resume_capture(self) -> None:
        """体检结束后恢复原采集（文件回放模式重建帧源，避免复用已关闭的 cap）。"""
        if self._file_path:
            self.set_file_source(self._file_path)
        if self.controller.start():
            self.btn_start.setText("暂停")
            self.status_line.setText("采集运行中")

    def _monitor_env(self) -> None:
        """持续监测（5s 定时）：采集中评估最近 ~1s 亮度/频闪，异常时横幅提示。"""
        if not self.controller.running or self.controller.paused:
            return
        problems = self.controller.evaluate_environment()
        if problems:
            text = "环境提醒：" + "；".join(
                f"{r.name} {r.measured}（{r.suggestion}）" if r.suggestion else f"{r.name} {r.measured}"
                for r in problems
            )
            self.env_banner.setText(text)
            self.env_banner.show()
        else:
            self.env_banner.hide()

    # ---- 控制 ----

    def _on_start_clicked(self) -> None:
        if not self.controller.running:
            if self.controller.start():
                self.btn_start.setText("暂停")
                self.status_line.setText("采集运行中")
        elif self.controller.paused:
            self.controller.resume()
            self.btn_start.setText("暂停")
            self.status_line.setText("采集继续")
        else:
            self.controller.pause()
            self.btn_start.setText("继续")
            self.status_line.setText("采集已暂停（预览保持）")

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
            self._file_path = None
            self.settings.camera_index = int(text.removeprefix(_SOURCE_UVC_PREFIX))
            self.settings.save()

    def set_file_source(self, path: str) -> None:
        """切到视频文件回放源（无相机演示模式）。"""
        self._file_path = path
        was_running = self.controller.running
        self.controller.close()  # 旧控制器整体关停（含落盘 worker 与语音）
        self.controller = CaptureController(
            self.settings, self.store, source=FileSource(path, loop=True)
        )
        self.attach_controller(self.controller)
        self.status_line.setText(f"文件回放：{Path(path).name}")
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
        self._env_timer.stop()
        self._countdown_timer.stop()
        self._no_swing_timer.stop()
        self.controller.close()
