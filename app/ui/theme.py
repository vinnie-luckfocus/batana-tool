"""macOS 原生风格设计系统（Apple HIG / macOS 26 Liquid Glass 观感）。

设计原则：
- 原生优先：控件默认交给 Qt 的 macOS 样式引擎渲染，QSS 只精修自定义组件
  （半透明卡片、状态 pill、分段控件、通知横幅、主操作按钮、列表选中态）；
- 真毛玻璃：窗口/侧栏背景由 NSVisualEffectView 提供（见 vibrancy.py），
  卡片用半透明面让模糊透出来，不用不透明面盖住 vibrancy；
- 跟随系统外观：浅色/深色由 Qt 自动跟随 macOS，表面色取 QPalette 系统角色，
  强调色与语义色取 macOS 系统色（浅/深两套，运行时按当前外观选择）；
- 表面质感：连续圆角 10–14px、0.5px 发丝级半透明描边、同一方向的着色软阴影
  （widgets.soft_shadow，正下方微偏移、统一模糊半径体系）；
- 字体：正文系统字体（SF Pro / 中文 PingFang SC），数值读数等宽数字字体；
- 禁 ASCII 装饰、禁全大写标题。
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

# 阴影体系（光照一致：正下方微偏移，统一模糊半径档位）
SHADOW_BLUR = 28      # 统一模糊半径
SHADOW_DY = 6         # 垂直偏移（光源来自正上方）


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


def title_font(size: int = 22) -> QFont:
    """页面/结论级标题：系统字体 semibold 观感，sentence case。"""
    font = QFont()
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.DemiBold)
    return font


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
    """按当前外观生成精修 QSS。

    卡片为半透明面（真机窗口服务下透出 NSVisualEffectView 的模糊；
    offscreen/无 PyObjC 时也有足够的近似质感）。
    """
    app = QGuiApplication.instance()
    palette = app.palette() if app is not None else QPalette()
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.Text)
    dark = is_dark_mode()
    # 卡片面：半透明（浅色外观近白、深色外观浅灰），让背后模糊透出来
    card = _rgba(QColor("#3A3A3C"), 150) if dark else _rgba(QColor("#FFFFFF"), 178)
    hairline = _rgba(text, 26 if dark else 18)           # 0.5px 观感的发丝描边
    dim = _rgba(text, 150)                                # 次级文字
    track = _rgba(text, 12 if dark else 16)               # 分段控件轨道底
    pill = _rgba(QColor("#5A5A5C"), 220) if dark else _rgba(QColor("#FFFFFF"), 245)
    accent = semantic_color("accent")
    acc_name = accent.name()
    sel_bg = _rgba(accent, 46 if dark else 36)            # 访达式圆角选中行
    return f"""
QWidget#card {{
    background-color: {card};
    border: 1px solid {hairline};
    border-radius: 12px;
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
    color: {acc_name};
}}
QLabel#warning {{
    color: {semantic_hex("orange")};
}}
QLabel#banner {{
    border-radius: 10px;
    padding: 8px 12px;
}}
/* 分段控件（仿 NSToolbar segmented control）：选中段白色浮起 + 发丝描边 */
QWidget#segmentedTrack {{
    background-color: {track};
    border-radius: 9px;
}}
QPushButton#segment {{
    background-color: transparent;
    border: none;
    border-radius: 7px;
    padding: 4px 18px;
    color: {dim};
}}
QPushButton#segment:hover {{
    background-color: {_rgba(text, 10)};
}}
QPushButton#segment:checked {{
    background-color: {pill};
    color: {text.name()};
    border: 1px solid {hairline};
}}
/* 主操作按钮（prominent）：系统蓝填充白字圆角 */
QPushButton#primary {{
    background-color: {acc_name};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 6px 16px;
}}
QPushButton#primary:hover {{
    background-color: {_rgba(accent, 225)};
}}
QPushButton#primary:pressed {{
    background-color: {_rgba(accent, 190)};
}}
QPushButton#primary:disabled {{
    background-color: {_rgba(accent, 90)};
}}
/* 列表：访达式半透明圆角选中行，hover 轻微浮色 */
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 6px;
    padding: 5px 8px;
}}
QListWidget::item:hover {{
    background-color: {_rgba(text, 12)};
}}
QListWidget::item:selected {{
    background-color: {sel_bg};
    color: {text.name()};
}}
/* 审核页侧栏：真机为 sidebar 材质毛玻璃；回退态给轻染底 + 右侧发丝分隔 */
QWidget#sidebarPanel {{
    background-color: {_rgba(text, 8)};
    border-right: 1px solid {hairline};
}}
QTableWidget {{
    border: 1px solid {hairline};
    border-radius: 10px;
}}
QToolTip {{
    border-radius: 6px;
    padding: 6px 8px;
}}
"""


def apply_theme(app) -> None:
    """把精修 QSS 挂到 QApplication；控件本身交给系统样式引擎渲染。

    同时监听系统浅色/深色切换（colorSchemeChanged），切换时重新生成 QSS；
    自绘组件通过各自的 paletteChange 事件重取语义色（见 widgets/preview/player）。
    """
    app.setStyleSheet(global_stylesheet())
    if not getattr(app, "_batana_appearance_watch", False):
        app._batana_appearance_watch = True
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: refresh_theme(app))


def refresh_theme(app) -> None:
    """外观切换时重新生成并挂载全局 QSS，并触发顶层窗口重绘。"""
    app.setStyleSheet(global_stylesheet())
    for window in app.topLevelWidgets():
        window.update()
