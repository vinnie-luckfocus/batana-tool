"""macOS 原生毛玻璃（NSVisualEffectView）集成：真模糊、真透光。

通过 PyObjC 把 NSVisualEffectView 插入 Qt 部件的 NSView 底部，
材质语义对齐 macOS 26（sidebar / popover / hudWindow / contentBackground 等）。

优雅回退：非 cocoa 平台（如 offscreen 测试）、未安装 pyobjc、
或指针包装失败时一律返回 False，调用方继续走 QSS 半透明近似渲染，
功能与测试不受影响。
"""

from __future__ import annotations

import logging
from ctypes import c_void_p

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

# NSVisualEffectMaterial 语义名 → (Cocoa 常量名, 枚举值兜底)
_MATERIALS = {
    "titlebar": ("NSVisualEffectMaterialTitlebar", 3),
    "menu": ("NSVisualEffectMaterialMenu", 5),
    "popover": ("NSVisualEffectMaterialPopover", 6),
    "sidebar": ("NSVisualEffectMaterialSidebar", 7),
    "headerView": ("NSVisualEffectMaterialHeaderView", 10),
    "sheet": ("NSVisualEffectMaterialSheet", 11),
    "windowBackground": ("NSVisualEffectMaterialWindowBackground", 12),
    "hudWindow": ("NSVisualEffectMaterialHUDWindow", 13),
    "toolTip": ("NSVisualEffectMaterialToolTip", 17),
    "contentBackground": ("NSVisualEffectMaterialContentBackground", 18),
    "underWindowBackground": ("NSVisualEffectMaterialUnderWindowBackground", 21),
}

_BLENDING = {"behindWindow": 0, "withinWindow": 1}


def vibrancy_available() -> bool:
    """当前环境是否可用真毛玻璃（cocoa 平台 + PyObjC 已安装）。"""
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        import Cocoa  # noqa: F401
        import objc  # noqa: F401
    except ImportError:
        return False
    return True


def _nsview_of(widget: QWidget):
    """QWidget.winId() → NSView 对象；失败返回 None。"""
    import objc

    ptr = int(widget.winId())
    if ptr <= 1:  # offscreen 等平台返回伪句柄
        return None
    try:
        view = objc.objc_object(c_void_p=c_void_p(ptr))
    except Exception:
        return None
    from Cocoa import NSView

    return view if isinstance(view, NSView) else None


def apply_vibrancy(
    widget: QWidget,
    material: str = "sidebar",
    blending: str = "behindWindow",
) -> bool:
    """在部件 NSView 底部插入 NSVisualEffectView，并把部件背景置为透明。

    material：sidebar（侧栏，访达观感）/ popover / hudWindow / contentBackground 等；
    blending：behindWindow（模糊窗口背后的桌面/内容，Liquid Glass 观感）
              或 withinWindow（只模糊窗口内下层内容）。
    返回是否成功；失败时调用方应使用 QSS 半透明近似兜底。
    """
    if not vibrancy_available():
        return False
    try:
        import Cocoa

        # 先声明透明背景再取 winId()：winId() 会立即创建平台窗口，
        # WA_TranslucentBackground 必须在 QNSWindow 创建之前设置才生效
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        nsview = _nsview_of(widget)
        if nsview is None:
            widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            return False
        effect = Cocoa.NSVisualEffectView.alloc().initWithFrame_(nsview.bounds())
        attr, fallback = _MATERIALS.get(material, _MATERIALS["sidebar"])
        value = getattr(Cocoa, attr, None)
        effect.setMaterial_(value if value is not None else fallback)
        effect.setBlendingMode_(_BLENDING.get(blending, 0))
        effect.setState_(Cocoa.NSVisualEffectStateActive)
        effect.setAutoresizingMask_(
            Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable
        )
        nsview.addSubview_positioned_relativeTo_(effect, Cocoa.NSWindowBelow, None)
        # Qt 6 cocoa 合成怪癖：全 alien 子部件的半透明顶层窗口不会把内容
        # 合成上屏（整窗空白）；强制一个后代部件转 native 即恢复合成路径。
        anchor = widget.findChild(QWidget)
        if anchor is not None:
            anchor.winId()
        return True
    except Exception as e:  # pragma: no cover - 依赖窗口服务器状态
        log.debug("vibrancy 应用失败，回退 QSS 半透明: %s", e)
        return False
