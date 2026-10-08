"""设计系统通用控件：半透明卡片、区段标题、遥测数值、状态 pill、迷你进度条、
相机指示、通知横幅、空状态引导视图、着色软阴影。

macOS 26 Liquid Glass 观感：连续圆角 10–14px、半透明表面透出背后模糊、
0.5px 发丝描边、同一方向（正下方微偏移）统一模糊半径的软阴影。
浅/深色运行时切换：带内联样式的控件实现 paletteChange 重取语义色。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import (
    SHADOW_BLUR,
    SHADOW_DY,
    is_dark_mode,
    mono_font,
    semantic_color,
    semantic_hex,
    tint,
    ui_font,
)


def _repaint_on_palette_change(widget: QWidget, event) -> bool:
    """paletteChange 时重绘自绘件；返回是否已处理。"""
    if event.type() == QEvent.Type.PaletteChange:
        widget.update()
        return True
    return False


class _RestyleGuard:
    """paletteChange 重挂内联样式时的重入保护（setStyleSheet 会再发 PaletteChange）。"""

    _restyling: bool = False

    def _restyle(self, apply) -> None:
        if self._restyling:
            return
        self._restyling = True
        try:
            apply()
        finally:
            self._restyling = False


def soft_shadow(widget: QWidget) -> None:
    """着色软阴影：正下方微偏移、统一模糊半径、带环境色偏色（非黑灰死阴影）。"""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(SHADOW_BLUR)
    effect.setOffset(0, SHADOW_DY)
    if is_dark_mode():
        effect.setColor(QColor(0, 0, 0, 120))
    else:
        effect.setColor(QColor(30, 45, 70, 36))  # 偏冷环境色
    widget.setGraphicsEffect(effect)


def card_widget(inner: QWidget | None = None, shadow: bool = False) -> QWidget:
    """圆角半透明卡片容器（0.5px 发丝描边由 QSS 提供；可选软阴影）。

    内边距统一 16px（8pt 网格）。
    频繁重绘的内容（如视频预览）不要开阴影，避免每次重绘走 effect 光栅化。
    """
    w = QWidget()
    w.setObjectName("card")
    if inner is not None:
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(inner)
    if shadow:
        soft_shadow(w)
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
        self.value.setMinimumWidth(96)  # 避免窄栏换行错位
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


class StateBanner(_RestyleGuard, QLabel):
    """状态机当前状态 pill：圆角胶囊，语义色半透明底 + 同色文字 + 内描边高光。

    READY 倒计时显示大号数字。
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
            self.setFont(ui_font(44, bold=True))
            self.setText(str(self._countdown))
        else:
            self.setFont(ui_font(24, bold=True))
            self.setText(STATE_LABELS.get(self._state, self._state))
        self.setStyleSheet(self._style())

    def _style(self) -> str:
        name = "red" if self._alarm else _STATE_COLORS.get(self._state, "fg_dim")
        edge = tint(name, 120)  # 内描边高光：同色更亮一档，模拟玻璃边缘折射
        return (
            f"color: {semantic_hex(name)};"
            f"background-color: {tint(name)};"
            f"border: 1px solid {edge};"
            "border-radius: 14px; padding: 12px 20px;"
        )

    def changeEvent(self, event) -> None:  # noqa: N802
        # 外观切换：pill 语义色随系统浅/深色重取
        if event.type() == QEvent.Type.PaletteChange:
            self._restyle(self._refresh)
        super().changeEvent(event)


class MiniBar(QWidget):
    """细圆角进度条（运动占比等）：强调色填充 + 阈值刻度竖线。"""

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
            painter.setBrush(semantic_color("accent"))
            painter.drawRoundedRect(0, int(y), max(h, int(w * frac)), h, h / 2, h / 2)
        # 触发阈值刻度：便于目测"还差多少触发"
        if self._threshold is not None and self._maximum > 0:
            x = int(w * min(max(self._threshold / self._maximum, 0.0), 1.0))
            tick = self.palette().color(self.foregroundRole())
            tick.setAlpha(160)
            painter.setPen(QPen(tick, 1.5))
            painter.drawLine(x, int(y) - 2, x, int(y) + h + 2)
        painter.end()

    def changeEvent(self, event) -> None:  # noqa: N802
        _repaint_on_palette_change(self, event)
        super().changeEvent(event)


class CameraIndicator(_RestyleGuard, QLabel):
    """相机连接状态指示：圆点 + 中文文案（系统绿=已连接）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFont(ui_font(12))
        self._connected = False
        self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        if connected:
            self.setText("● 相机已连接")
            self.setStyleSheet(f"color: {semantic_hex('green')};")
        else:
            self.setText("○ 相机未连接")
            self.setStyleSheet(f"color: {semantic_hex('fg_dim')};")

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange:
            self._restyle(lambda: self.set_connected(self._connected))  # 重取语义色
        super().changeEvent(event)


class NotificationBanner(_RestyleGuard, QLabel):
    """macOS 通知横幅式提示条（环境异常等）：圆角、暖色半透明底、自动随逻辑消隐。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("banner")
        self.setWordWrap(True)
        self.setFont(ui_font(12))
        self._apply_style()
        self.hide()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"color: {semantic_hex('orange')}; background-color: {tint('orange', 36)};"
            f"border: 1px solid {tint('orange', 110)};"
        )

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange:
            self._restyle(self._apply_style)
        super().changeEvent(event)


class EmptyStateView(QWidget):
    """空状态引导视图（仿系统设置空态）：图标位 + 标题 + 说明 + 可选行动按钮。

    图标位用系统样式标准图标（QStyle standardIcon），不引入外部素材。
    """

    def __init__(
        self,
        title: str,
        description: str,
        icon=None,
        action_text: str | None = None,
        on_action=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)
        layout.addStretch(1)
        if icon is not None:
            icon_label = QLabel()
            icon_label.setPixmap(icon.pixmap(48, 48))
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setEnabled(False)  # 系统灰禁用态，弱化存在感
            layout.addWidget(icon_label)
            layout.addSpacing(4)
        title_label = QLabel(title)
        title_label.setFont(ui_font(15, bold=True))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        desc_label = QLabel(description)
        desc_label.setObjectName("dim")
        desc_label.setFont(ui_font(13))
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        if action_text and on_action is not None:
            from PySide6.QtWidgets import QPushButton

            layout.addSpacing(8)
            btn = QPushButton(action_text)
            btn.setObjectName("primary")
            btn.clicked.connect(on_action)
            row = QVBoxLayout()
            row.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
            layout.addLayout(row)
        layout.addStretch(1)
