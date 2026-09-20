"""设计系统通用控件：ASCII 区段头、遥测数值、迷你条形、相机连接指示。

所有控件直角、等宽字体、大写文本；1px 网格缝由布局（父背景 #0A0A0A +
子块 objectName=block + spacing 1）实现，见各页组装代码。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.theme import COLORS, header_font, mono_font


def block_widget(inner: QWidget | None = None) -> QWidget:
    """#121212 子块容器（与父布局 1px 间距形成网格缝）。"""
    w = QWidget()
    w.setObjectName("block")
    if inner is not None:
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(inner)
    return w


class SectionHeader(QLabel):
    """ASCII 装饰区段头，如 `[ CAPTURE ]` / `>>> SESSION 012`。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)
        self.setFont(mono_font(10, bold=True, letter_spacing=2.5))
        self.setProperty("class", "section")


class TelemetryValue(QWidget):
    """遥测数值块：上行小标签（暗色），下行大数值（等宽大写）。"""

    def __init__(self, label: str, value: str = "--", accent: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.label = QLabel(label.upper())
        self.label.setObjectName("dim")
        self.label.setFont(mono_font(9, letter_spacing=2.0))
        self.value = QLabel(value.upper())
        self.value.setFont(mono_font(16, bold=True, letter_spacing=1.5))
        if accent:
            self.value.setObjectName("accent")
        layout.addWidget(self.label)
        layout.addWidget(self.value)

    def set_value(self, value: str) -> None:
        self.value.setText(value.upper())


# 状态机状态 → 中文语义（H1：3 米外/静音环境下"现在该做什么"一眼可读）
STATE_LABELS = {
    "IDLE": "等待就位",
    "READY": "请准备",
    "ARMED": "请挥棒！",
    "SWING": "录制中",
    "SAVING": "保存中",
    "ERROR": "异常",
}


class StateBanner(QLabel):
    """状态机当前状态大字横幅：中文语义 + 分色 + READY 倒计时大号数字。

    分色（受设计系统调色板约束）：IDLE/SAVING 暗灰、READY 亮白、
    ARMED/ERROR 强调红文字、SWING 红底白字（录制中最强视觉信号）。
    """

    def __init__(self, text: str = "IDLE") -> None:
        super().__init__("")
        self._state = "IDLE"
        self._alarm = False
        self._countdown: int | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state(text)

    def set_state(self, name: str, alarm: bool = False) -> None:
        """设置状态（英文状态名自动映射中文）；离开 READY 时清除倒计时。"""
        self._state = name
        self._alarm = alarm
        if name != "READY":
            self._countdown = None
        self._refresh()

    def set_countdown(self, n: int | None) -> None:
        """READY 倒计时大号数字（3/2/1）；None 恢复状态文案。"""
        self._countdown = n
        self._refresh()

    def _refresh(self) -> None:
        if self._countdown is not None:
            self.setFont(header_font(56))
            self.setText(str(self._countdown))
        else:
            self.setFont(header_font(34))
            self.setText(STATE_LABELS.get(self._state, self._state))
        self.setStyleSheet(self._style())

    def _style(self) -> str:
        base = f"border-top: 2px solid {COLORS['accent']}; padding: 8px;"
        if self._state == "SWING":
            return f"color: {COLORS['fg']}; background-color: {COLORS['accent']}; {base}"
        if self._alarm or self._state in ("ARMED", "ERROR"):
            color = COLORS["accent"]
        elif self._state in ("IDLE", "SAVING"):
            color = COLORS["fg_dim"]
        else:
            color = COLORS["fg"]
        return f"color: {color}; {base}"


class MiniBar(QWidget):
    """微型条形指示（运动能量等）：1px 描边直角，填充用强调色，可选阈值刻度竖线。"""

    def __init__(self, maximum: float = 40.0, threshold: float | None = None) -> None:
        super().__init__()
        self._maximum = maximum
        self._value = 0.0
        self._threshold = threshold
        self.setMinimumHeight(10)

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(value, self._maximum))
        self.update()

    def set_threshold(self, value: float | None) -> None:
        """触发阈值刻度竖线位置（与 set_value 同一量纲）。"""
        self._threshold = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802（Qt 命名）
        painter = QPainter(self)
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        frac = self._value / self._maximum if self._maximum > 0 else 0.0
        if frac > 0:
            painter.fillRect(
                1, 1, int((self.width() - 2) * frac), self.height() - 2,
                QColor(COLORS["accent"]),
            )
        # 触发阈值刻度：亮白竖线，便于目测"还差多少触发"
        if self._threshold is not None and self._maximum > 0:
            x = 1 + int((self.width() - 2) * min(max(self._threshold / self._maximum, 0.0), 1.0))
            painter.setPen(QPen(QColor(COLORS["fg"]), 1))
            painter.drawLine(x, 0, x, self.height() - 1)
        painter.end()


class CameraIndicator(QLabel):
    """相机连接状态指示——终端绿 #4AF626 在全 UI 中的唯一用途。"""

    def __init__(self) -> None:
        super().__init__("CAM [ OFF ]")
        self.setFont(mono_font(10, bold=True, letter_spacing=2.0))
        self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        if connected:
            self.setText("CAM [ ON ]")
            self.setStyleSheet(f"color: {COLORS['terminal']};")
        else:
            self.setText("CAM [ OFF ]")
            self.setStyleSheet(f"color: {COLORS['fg_dim']};")


class CrossMark(QWidget):
    """十字准线 `+` 装饰（网格交点用）。"""

    def __init__(self, size: int = 11) -> None:
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(QPen(QColor(COLORS["fg_dim"]), 1))
        cx, cy = self.width() // 2, self.height() // 2
        painter.drawLine(cx, 0, cx, self.height() - 1)
        painter.drawLine(0, cy, self.width() - 1, cy)
        painter.end()
