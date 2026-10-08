"""采集预览控件：双目画面显示 + ROI 框选 + 水平参考线（PRD F2）。

FrameView 为画面坐标映射基类（审核页回放器复用）；
PreviewWidget 叠加：ROI 系统红描边 + 半透明填充 + 四角手柄、虚线水平参考线。
ROI 以左目像素坐标存储与发射；鼠标拖拽框选，释放时发 roi_changed。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.ui.theme import semantic_color, ui_font

RoiTuple = tuple[int, int, int, int]

VIEW_LEFT = "left"
VIEW_RIGHT = "right"
VIEW_SBS = "sbs"


def gray_to_qimage(frame: np.ndarray) -> QImage:
    """MONO8 灰度帧 → QImage（拷贝持有，与 numpy 缓冲解耦）。"""
    if frame.ndim == 3:
        frame = frame[:, :, 0]
    frame = np.ascontiguousarray(frame)
    h, w = frame.shape
    return QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8).copy()


class FrameView(QWidget):
    """画面显示基类：等比缩放居中，提供 控件坐标 ↔ 图像像素坐标 映射。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._content = QRectF()
        self.setMinimumSize(320, 200)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def image_size(self) -> tuple[int, int]:
        if self._image is None:
            return (0, 0)
        return (self._image.width(), self._image.height())

    # ---- 坐标映射 ----

    def _update_content_rect(self) -> None:
        if self._image is None or self._image.width() == 0:
            self._content = QRectF()
            return
        iw, ih = self._image.width(), self._image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        self._content = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def widget_to_image(self, pos: QPointF) -> tuple[float, float]:
        """控件坐标 → 图像像素坐标（未截断，调用方自行 clamp）。"""
        self._update_content_rect()
        if self._content.isEmpty():
            return (0.0, 0.0)
        iw, ih = self.image_size()
        x = (pos.x() - self._content.x()) / self._content.width() * iw
        y = (pos.y() - self._content.y()) / self._content.height() * ih
        return (x, y)

    def image_to_widget(self, x: float, y: float) -> QPointF:
        self._update_content_rect()
        iw, ih = self.image_size()
        if iw == 0 or ih == 0:
            return QPointF()
        return QPointF(
            self._content.x() + x / iw * self._content.width(),
            self._content.y() + y / ih * self._content.height(),
        )

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))  # 画面区标准黑底
        self._update_content_rect()
        if self._image is not None:
            painter.drawImage(self._content, self._image)
        else:
            dim = self.palette().color(self.foregroundRole())
            dim.setAlpha(120)
            painter.setPen(dim)
            painter.setFont(ui_font(13))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无信号")
        self.paint_overlay(painter)
        painter.end()

    def paint_overlay(self, painter: QPainter) -> None:
        """子类叠加层钩子（ROI / 骨架等）。"""

    def changeEvent(self, event) -> None:  # noqa: N802
        # 外观切换：叠加层语义色随系统浅/深色重取（paint 时实时读色）
        if event.type() == QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)


class PreviewWidget(FrameView):
    """采集预览：左/右/双目并排切换 + ROI 框选（红色 1px + 四角十字）。"""

    roi_changed = Signal(int, int, int, int)  # (x, y, w, h) 左目像素坐标

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_mode = VIEW_LEFT
        self._left_np: np.ndarray | None = None
        self._right_np: np.ndarray | None = None
        self._roi: RoiTuple | None = None
        self._drag_start: tuple[float, float] | None = None
        self._drag_current: tuple[float, float] | None = None

    # ---- 数据源 ----

    def set_frames(self, left: np.ndarray | None, right: np.ndarray | None = None) -> None:
        self._left_np = left
        self._right_np = right
        self._refresh_image()

    def set_view_mode(self, mode: str) -> None:
        if mode in (VIEW_LEFT, VIEW_RIGHT, VIEW_SBS):
            self._view_mode = mode
            self._refresh_image()

    def view_mode(self) -> str:
        return self._view_mode

    def _refresh_image(self) -> None:
        if self._view_mode == VIEW_RIGHT:
            frame = self._right_np
        elif self._view_mode == VIEW_SBS and self._left_np is not None and self._right_np is not None:
            frame = np.concatenate([self._left_np, self._right_np], axis=1)
        else:
            frame = self._left_np
        self.set_image(gray_to_qimage(frame) if frame is not None else None)

    # ---- ROI ----

    def set_roi(self, roi: RoiTuple | None) -> None:
        self._roi = roi
        self.update()

    def roi(self) -> RoiTuple | None:
        return self._roi

    def _eye_origin(self) -> tuple[float, float]:
        """当前视图下左目原点在图像像素坐标中的偏移（SBS 模式左目在左半）。"""
        return (0.0, 0.0)

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        """截断到左目范围。"""
        iw, ih = self.image_size()
        eye_w = iw / 2 if self._view_mode == VIEW_SBS else iw
        return (min(max(x, 0.0), eye_w), min(max(y, 0.0), float(ih)))

    # ---- 鼠标框选 ----

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._image is not None:
            x, y = self.widget_to_image(event.position())
            self._drag_start = self._clamp(x, y)
            self._drag_current = self._drag_start
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_start is not None:
            x, y = self.widget_to_image(event.position())
            self._drag_current = self._clamp(x, y)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            x, y = self.widget_to_image(event.position())
            self._drag_current = self._clamp(x, y)
            rect = self._drag_rect()
            self._drag_start = None
            self._drag_current = None
            if rect is not None and rect[2] >= 4 and rect[3] >= 4:
                self._roi = rect
                self.roi_changed.emit(*rect)
            self.update()

    def _drag_rect(self) -> RoiTuple | None:
        if self._drag_start is None or self._drag_current is None:
            return None
        x0, y0 = self._drag_start
        x1, y1 = self._drag_current
        x, y = min(x0, x1), min(y0, y1)
        return (int(x), int(y), int(abs(x1 - x0)), int(abs(y1 - y0)))

    # ---- 叠加绘制 ----

    def paint_overlay(self, painter: QPainter) -> None:
        if self._image is None:
            return
        # 水平参考线（虚线，辅助调平）：半透明白，弱存在感
        iw, ih = self.image_size()
        if ih > 0:
            p0 = self.image_to_widget(0, ih / 2)
            p1 = self.image_to_widget(iw, ih / 2)
            guide = QColor(255, 255, 255, 70)
            pen = QPen(guide, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(p0, p1)
        # ROI：常显已存 ROI 或拖拽中的临时框
        roi = self._drag_rect() if self._drag_start is not None else self._roi
        if roi is not None:
            self._draw_roi(painter, roi)

    def _draw_roi(self, painter: QPainter, roi: RoiTuple) -> None:
        x, y, w, h = roi
        p0 = self.image_to_widget(x, y)
        p1 = self.image_to_widget(x + w, y + h)
        rect = QRectF(p0, p1)
        red = semantic_color("red")
        # 系统红 2px 描边 + 半透明填充
        fill = QColor(red)
        fill.setAlpha(50)
        painter.fillRect(rect, fill)
        painter.setPen(QPen(red, 2))
        painter.drawRect(rect)
        # 四角方形手柄（选中框语义）
        handle = 7.0
        painter.setBrush(red)
        painter.setPen(Qt.PenStyle.NoPen)
        for cx, cy in ((rect.left(), rect.top()), (rect.right(), rect.top()),
                       (rect.left(), rect.bottom()), (rect.right(), rect.bottom())):
            painter.drawRect(QRectF(cx - handle / 2, cy - handle / 2, handle, handle))
