"""app.ui：PySide6 桌面界面——采集页、审核页、设置页（战术遥测/CRT 终端风格）。

设计系统实现集中在 theme.py；核心层（capture/detect/pose/voice/session）不依赖 Qt。
"""

from app.ui.settings import AppSettings
from app.ui.theme import COLORS, apply_theme

__all__ = ["AppSettings", "COLORS", "apply_theme"]
