"""战术遥测 / CRT 终端设计系统：配色、字体、全局 QSS。

硬性规则（见 PRD 界面要求）：
- 背景 #0A0A0A / #121212（禁纯黑），前景 #EAEAEA；
- 唯一强调色 #E61919（警示 / 关键数据 / 状态分割线）；
- 终端绿 #4AF626 只允许用于"相机连接状态"单一指示；
  （例外：回放画面内的骨架置信度着色属于数据可视化叠加层，见 player.py）
- 禁渐变、禁圆角（全部直角）、禁柔和阴影；
- 数据/遥测一律等宽（JetBrains Mono，回退 Menlo/等宽），10–14px、字距加宽、大写；
- 结构性大标题用粗黑无衬线（Helvetica Neue Bold / Arial Black）、大写、负字距；
- 布局分隔用 1px 网格缝（父背景深色 + 子块 #121212 + spacing 1px）。
"""

from __future__ import annotations

from PySide6.QtGui import QFont

# ---- 配色 ----
COLORS = {
    "bg_deep": "#0A0A0A",    # 应用底色 / 网格缝
    "bg_block": "#121212",   # 子块背景
    "bg_raise": "#1A1A1A",   # 输入框 / 列表行 hover 底
    "fg": "#EAEAEA",         # 主前景
    "fg_dim": "#7A7A7A",     # 次级文字 / 标签
    "accent": "#E61919",     # 唯一强调色（红）
    "terminal": "#4AF626",   # 终端绿（仅相机连接指示）
    "border": "#2A2A2A",     # 控件内描边
}

# ---- 字体族 ----
MONO_FAMILIES = ["JetBrains Mono", "Menlo", "SF Mono", "Courier New", "monospace"]
HEADER_FAMILIES = ["Helvetica Neue", "Arial Black", "Arial", "sans-serif"]


def mono_font(size: int = 11, bold: bool = False, letter_spacing: float = 1.5) -> QFont:
    """遥测等宽字体：加宽字距，调用方负责 .upper() 大写文本。"""
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPixelSize(size)
    font.setBold(bold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    return font


def header_font(size: int = 28) -> QFont:
    """结构性大标题：粗黑无衬线、负字距（文本需大写）。"""
    font = QFont()
    font.setFamilies(HEADER_FAMILIES)
    font.setPixelSize(size)
    font.setBold(True)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 96.0)
    return font


def global_stylesheet() -> str:
    """全局 QSS：直角、无渐变、无阴影，1px 深色缝分隔由布局 spacing 实现。"""
    c = COLORS
    return f"""
QWidget {{
    background-color: {c['bg_deep']};
    color: {c['fg']};
    border: none;
    selection-background-color: {c['accent']};
    selection-color: {c['fg']};
}}
QWidget#block {{
    background-color: {c['bg_block']};
}}
QLabel {{
    background-color: transparent;
}}
QLabel#dim {{
    color: {c['fg_dim']};
}}
QLabel#accent {{
    color: {c['accent']};
}}
QLabel#terminal {{
    color: {c['terminal']};
}}
QPushButton {{
    background-color: {c['bg_block']};
    color: {c['fg']};
    border: 1px solid {c['border']};
    border-radius: 0px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    border: 1px solid {c['fg_dim']};
}}
QPushButton:pressed {{
    background-color: {c['accent']};
    color: {c['fg']};
}}
QPushButton:disabled {{
    color: {c['fg_dim']};
    border: 1px solid {c['border']};
}}
QPushButton#primary {{
    background-color: {c['accent']};
    color: {c['fg']};
    border: 1px solid {c['accent']};
}}
QPushButton#primary:pressed {{
    background-color: {c['fg']};
    color: {c['bg_deep']};
}}
QPushButton#tab {{
    background-color: {c['bg_deep']};
    border: none;
    border-bottom: 2px solid {c['bg_deep']};
    padding: 8px 20px;
}}
QPushButton#tab:checked {{
    border-bottom: 2px solid {c['accent']};
    color: {c['accent']};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {c['bg_raise']};
    color: {c['fg']};
    border: 1px solid {c['border']};
    border-radius: 0px;
    padding: 4px 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {c['bg_block']};
    border: 1px solid {c['border']};
    outline: none;
}}
QListWidget, QTableWidget, QTreeWidget {{
    background-color: {c['bg_block']};
    border: none;
    outline: none;
    gridline-color: {c['bg_deep']};
}}
QListWidget::item, QTableWidget::item {{
    padding: 4px 8px;
    border-bottom: 1px solid {c['bg_deep']};
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {c['bg_raise']};
    color: {c['accent']};
    border-left: 2px solid {c['accent']};
}}
QSlider::groove:horizontal {{
    height: 2px;
    background: {c['border']};
}}
QSlider::handle:horizontal {{
    width: 10px;
    height: 16px;
    margin: -7px 0;
    background: {c['accent']};
    border-radius: 0px;
}}
QSlider::sub-page:horizontal {{
    background: {c['accent']};
}}
QProgressBar {{
    background-color: {c['bg_raise']};
    border: 1px solid {c['border']};
    border-radius: 0px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {c['accent']};
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
    background-color: transparent;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {c['border']};
    border-radius: 0px;
    background-color: {c['bg_raise']};
}}
QCheckBox::indicator:checked {{
    background-color: {c['accent']};
    border: 1px solid {c['accent']};
}}
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 0px;
    margin-top: 14px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    color: {c['fg_dim']};
}}
QScrollBar:vertical {{
    background: {c['bg_deep']};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c['border']};
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {c['bg_deep']};
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {c['border']};
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QToolTip {{
    background-color: {c['bg_block']};
    color: {c['fg']};
    border: 1px solid {c['accent']};
    padding: 4px;
}}
QFileDialog, QMessageBox {{
    background-color: {c['bg_deep']};
}}
"""


def apply_theme(widget) -> None:
    """把全局 QSS 挂到 QApplication（或顶层部件）。"""
    widget.setStyleSheet(global_stylesheet())
