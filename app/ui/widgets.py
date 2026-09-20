"""设计系统通用控件：卡片容器、区段标题、遥测数值、状态 pill、迷你进度条、相机指示。

macOS 原生风格：圆角卡片面、次级灰标签、数值用等宽数字字体；
StateBanner 为圆角状态 pill（语义色：绿=就绪、蓝=进行中、红=异常/挥棒提示）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.theme import mono_font, semantic_hex, tint, ui_font


def card_widget(inner: QWidget | None = None) -> QWidget:
    """圆角卡片容器（比窗口浅一级的面，替代旧 1px 网格缝子块）。"""
    w = QWidget()
    w.setObjectName("card")
    if inner is not None:
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addWidget(inner)
    return w


# 兼容旧导入名（页面代码统一使用卡片容器）
block_widget = card_widget


class SectionHeader(QLabel):
    """区段标题：加粗 sentence case 中文，不做 ASCII 装饰与全大写。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setFont(ui_font(13, bold=True))


class TelemetryValue(QWidget):
    """遥测数值块：上行次级小标签，下行等宽数字读数。"""

    def __init__(self, label: str, value: str = "--", accent: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.label = QLabel(label)
        self.label.setObjectName("dim")
        self.label.setFont(ui_font(11))
        self.value = QLabel(value)
        self.value.setFont(mono_font(15))
        if accent:
            self.value.setObjectName("accent")
        layout.addWidget(self.label)
        layout.addWidget(self.value)

    def set_value(self, value: str) -> None:
        self.value.setText(value)


# 状态机状态 → 中文语义（H1：3 米外/静音环境下"现在该做什么"一眼可读）
STATE_LABELS = {
    "IDLE": "等待就位",
    "READY": "请准备",
    "ARMED": "请挥棒！",
    "SWING": "录制中",
    "SAVING": "保存中",
    "ERROR": "异常",
}

# 状态 → 语义色：绿=就绪、蓝=进行中、红=异常/挥棒提示、灰=空闲
_STATE_COLORS = {
    "IDLE": "fg_dim",
    "READY": "green",
    "ARMED": "red",
    "SWING": "blue",
    "SAVING": "blue",
    "ERROR": "red",
}


class StateBanner(QLabel):
    """状态机当前状态 pill：圆角胶囊，语义色半透明底 + 同色文字，READY 倒计时大号数字。"""

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
            self.setFont(ui_font(44, bold=True))
            self.setText(str(self._countdown))
        else:
            self.setFont(ui_font(24, bold=True))
            self.setText(STATE_LABELS.get(self._state, self._state))
        self.setStyleSheet(self._style())

    def _style(self) -> str:
        name = "red" if self._alarm else _STATE_COLORS.get(self._state, "fg_dim")
        return (
            f"color: {semantic_hex(name)};"
            f"background-color: {tint(name)};"
            "border-radius: 12px; padding: 10px 18px;"
        )


class MiniBar(QWidget):
    """细圆角进度条（运动能量等）：强调色填充 + 阈值刻度竖线。"""

    def __init__(self, maximum: float = 40.0, threshold: float | None = None) -> None:
        super().__init__()
        self._maximum = maximum
        self._value = 0.0
        self._threshold = threshold
        self.setMinimumHeight(8)

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(value, self._maximum))
        self.update()

    def set_threshold(self, value: float | None) -> None:
        """触发阈值刻度竖线位置（与 set_value 同一量纲）。"""
        self._threshold = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802（Qt 命名）
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = 6
        y = (self.height() - h) / 2
        w = self.width()
        # 轨道：半透明灰
        track = self.palette().color(self.foregroundRole())
        track.setAlpha(30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(0, int(y), w, h, h / 2, h / 2)
        # 填充：系统强调色
        frac = self._value / self._maximum if self._maximum > 0 else 0.0
        if frac > 0:
            painter.setBrush(QColor(semantic_hex("accent")))
            painter.drawRoundedRect(0, int(y), max(h, int(w * frac)), h, h / 2, h / 2)
        # 触发阈值刻度：便于目测"还差多少触发"
        if self._threshold is not None and self._maximum > 0:
            x = int(w * min(max(self._threshold / self._maximum, 0.0), 1.0))
            tick = self.palette().color(self.foregroundRole())
            tick.setAlpha(160)
            painter.setPen(QPen(tick, 1.5))
            painter.drawLine(x, int(y) - 2, x, int(y) + h + 2)
        painter.end()


class CameraIndicator(QLabel):
    """相机连接状态指示：圆点 + 中文文案（系统绿=已连接）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFont(ui_font(12))
        self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        if connected:
            self.setText("● 相机已连接")
            self.setStyleSheet(f"color: {semantic_hex('green')};")
        else:
            self.setText("○ 相机未连接")
            self.setStyleSheet(f"color: {semantic_hex('fg_dim')};")


class NotificationBanner(QLabel):
    """macOS 通知横幅式提示条（环境异常等）：圆角、暖色半透明底、自动随逻辑消隐。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("banner")
        self.setWordWrap(True)
        self.setFont(ui_font(12))
        self.setStyleSheet(
            f"color: {semantic_hex('orange')}; background-color: {tint('orange', 36)};"
        )
        self.hide()
