"""macOS 原生风格设计系统（Apple HIG）。

设计原则：
- 原生优先：控件默认交给 Qt 的 macOS 样式引擎渲染，QSS 只用于自定义组件
  （卡片容器、状态 pill、分段控件、通知横幅）与少量精修，不覆盖系统控件外观；
- 跟随系统外观：浅色/深色由 Qt 自动跟随 macOS，表面色取 QPalette 系统角色，
  强调色与语义色取 macOS 系统色（浅/深两套，运行时按当前外观选择）；
- 字体：正文用系统字体（SF Pro，Qt 默认；中文 PingFang SC 自动回退），
  数值读数用等宽数字字体（SF Mono / Menlo），标题加粗、sentence case；
- 表面：圆角 8px 卡片（比窗口浅一级），发丝级半透明分隔，无堆叠阴影；
- 禁 ASCII 装饰、禁全大写标题、禁等宽正文。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette

# ---- 语义色（macOS 系统色，浅色外观基准值）----
# accent = 系统蓝；状态语义：绿=就绪、蓝=进行中、红=异常/挥棒提示
COLORS = {
    "accent": "#007AFF",
    "blue": "#007AFF",
    "green": "#28CD41",
    "red": "#FF3B30",
    "orange": "#FF9500",
    "yellow": "#FFCC00",
    "fg": "#1D1D1F",
    "fg_dim": "#86868B",
}

# 深色外观对应值
_COLORS_DARK = {
    "accent": "#0A84FF",
    "blue": "#0A84FF",
    "green": "#30D158",
    "red": "#FF453A",
    "orange": "#FF9F0A",
    "yellow": "#FFD60A",
    "fg": "#F5F5F7",
    "fg_dim": "#98989D",
}

# 等宽数字字体族（仅用于遥测/计数等数值读数）
MONO_FAMILIES = ["SF Mono", "Menlo", "monospace"]


def is_dark_mode() -> bool:
    """当前外观是否深色：优先 styleHints().colorScheme()，回退窗口底色亮度。"""
    app = QGuiApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    window = app.palette().color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def semantic_color(name: str) -> QColor:
    """按当前外观取语义色（浅色外观与 COLORS 字典一致）。"""
    table = _COLORS_DARK if is_dark_mode() else COLORS
    return QColor(table[name])


def semantic_hex(name: str) -> str:
    """semantic_color 的十六进制字符串形式（拼 QSS 用）。"""
    return semantic_color(name).name()


def tint(name: str, alpha: int = 40) -> str:
    """语义色的半透明填充色（pill / 横幅底色），返回 rgba() 字符串。"""
    c = semantic_color(name)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


# ---- 字体 ----


def ui_font(size: int = 13, bold: bool = False) -> QFont:
    """系统字体（SF Pro，Qt 默认族即系统字体；中文自动回退 PingFang SC）。"""
    font = QFont()
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def title_font(size: int = 20) -> QFont:
    """区段/结论标题：系统字体加粗，sentence case。"""
    return ui_font(size, bold=True)


def mono_font(size: int = 13, bold: bool = False) -> QFont:
    """等宽数字字体：仅用于帧率/能量/计数等数值读数，避免数字跳动。"""
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPixelSize(size)
    font.setBold(bold)
    return font


# ---- 全局 QSS（只精修自定义组件，系统控件交给 macOS 样式引擎）----


def _rgba(c: QColor, alpha: int) -> str:
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def global_stylesheet() -> str:
    """按当前外观生成精修 QSS：卡片面、次级文字、分段控件、通知横幅等。"""
    app = QGuiApplication.instance()
    palette = app.palette() if app is not None else QPalette()
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.Text)
    dark = is_dark_mode()
    # 卡片面：比窗口浅一级（浅色外观下接近纯白）
    card = window.lighter(112) if dark else QColor("#FFFFFF")
    hairline = _rgba(text, 28 if dark else 22)          # 发丝级分隔线
    dim = _rgba(text, 150)                               # 次级文字
    track = _rgba(text, 14 if dark else 18)              # 分段控件轨道底
    accent = semantic_hex("accent")
    return f"""
QWidget#card {{
    background-color: {card.name()};
    border-radius: 8px;
}}
QFrame#hairline {{
    background-color: {hairline};
    max-height: 1px;
    border: none;
}}
QLabel#dim {{
    color: {dim};
}}
QLabel#accent {{
    color: {accent};
}}
QWidget#segmentedTrack {{
    background-color: {track};
    border-radius: 7px;
}}
QPushButton#segment {{
    background-color: transparent;
    border: none;
    border-radius: 5px;
    padding: 3px 16px;
    color: {dim};
}}
QPushButton#segment:checked {{
    background-color: {card.name()};
    color: {text.name()};
}}
QLabel#banner {{
    border-radius: 8px;
    padding: 8px 12px;
}}
QToolTip {{
    border-radius: 6px;
    padding: 6px 8px;
}}
"""


def apply_theme(app) -> None:
    """把精修 QSS 挂到 QApplication；控件本身交给系统样式引擎渲染。"""
    app.setStyleSheet(global_stylesheet())
