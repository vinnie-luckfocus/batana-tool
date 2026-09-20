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


class StateBanner(QLabel):
    """状态机当前状态大字横幅（结构性大标题字体 + 强调色分割线上沿）。"""

    def __init__(self, text: str = "IDLE") -> None:
        super().__init__(text)
        self.setFont(header_font(34))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"color: {COLORS['fg']}; border-top: 2px solid {COLORS['accent']}; padding: 8px;"
        )

    def set_state(self, text: str, alarm: bool = False) -> None:
        self.setText(text.upper())
        color = COLORS["accent"] if alarm else COLORS["fg"]
        self.setStyleSheet(
            f"color: {color}; border-top: 2px solid {COLORS['accent']}; padding: 8px;"
        )


class MiniBar(QWidget):
    """微型条形指示（运动能量等）：1px 描边直角，填充用强调色。"""

    def __init__(self, maximum: float = 40.0) -> None:
        super().__init__()
        self._maximum = maximum
        self._value = 0.0
        self.setMinimumHeight(10)

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(value, self._maximum))
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
