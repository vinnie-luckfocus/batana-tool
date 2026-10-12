"""审核回放器：素材帧读取（ClipPlayer）+ 骨架叠加显示与拖动修正（PlayerWidget）。

叠加层视觉规范：
- 连线按身体侧着色（左橙 / 右蓝 / 中线灰）——快速区分左右肢体
- 关键点十字按置信度着色（高 ≥0.7 绿 / 中 0.4–0.7 黄 / 低 <0.4 红）——定位错点
- 悬停高亮 + 中文关节名标签——分清点对应的关节；手动修正点显示方框
- 简化手部模式：隐藏 pinky/index/thumb 细节点（数据仍保留 33 点全量）
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.pose import PoseFrame, read_pose2d
from app.ui.preview import FrameView, gray_to_qimage
from app.ui.theme import semantic_color, ui_font

# BlazePose 33 点连线（MediaPipe POSE_CONNECTIONS，索引对齐 KEYPOINT_NAMES）
POSE_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10), (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21),
    (17, 19), (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
    (27, 29), (28, 30), (29, 31), (30, 31), (27, 31), (28, 32),
]

# 手部细节点（简化手部模式下隐藏：不绘制、不命中；数据仍保留 33 点全量）
HAND_DETAIL_INDICES = frozenset({17, 18, 19, 20, 21, 22})  # 左右 pinky/index/thumb

# 悬停/拖动提示用中文关节名（索引对齐 KEYPOINT_NAMES）
KEYPOINT_LABELS_CN = [
    "鼻", "左眼内", "左眼", "左眼外", "右眼内", "右眼", "右眼外",
    "左耳", "右耳", "左嘴角", "右嘴角",
    "左肩", "右肩", "左肘", "右肘", "左腕", "右腕",
    "左小指", "右小指", "左食指", "右食指", "左拇指", "右拇指",
    "左髋", "右髋", "左膝", "右膝", "左踝", "右踝",
    "左脚跟", "右脚跟", "左脚尖", "右脚尖",
]
assert len(KEYPOINT_LABELS_CN) == 33

# 身体侧分组（连线着色：左橙 / 右蓝 / 中线灰，快速区分左右肢体）
_LEFT_SIDE = frozenset({1, 2, 3, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31})
_RIGHT_SIDE = frozenset({4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32})


def confidence_color(visibility: float) -> QColor:
    """置信度着色：高=系统绿 / 中=系统黄 / 低=系统红（随当前外观取色）。"""
    if visibility >= 0.7:
        return semantic_color("green")
    if visibility >= 0.4:
        return semantic_color("yellow")
    return semantic_color("red")


class ClipPlayer:
    """素材帧读取器：左右目视频懒解码（逐帧 seek + LRU 缓存），不依赖 Qt。"""

    def __init__(self, clip_dir: str | Path, frame_count: int, fps: float) -> None:
        self.clip_dir = Path(clip_dir)
        self.frame_count = int(frame_count)
        self.fps = float(fps)
        self._caps: dict[str, cv2.VideoCapture] = {}
        self._cache: OrderedDict[tuple[str, int], np.ndarray] = OrderedDict()
        self._cache_limit = 64
        for eye in ("left", "right"):
            matches = sorted(self.clip_dir.glob(f"{eye}.*"))
            if matches:
                cap = cv2.VideoCapture(str(matches[0]))
                if cap.isOpened():
                    self._caps[eye] = cap

    def eyes(self) -> list[str]:
        return list(self._caps.keys())

    def frame(self, index: int, eye: str = "left") -> np.ndarray | None:
        """读取第 index 帧（闭区间 [0, frame_count-1]），无此目返回 None。"""
        cap = self._caps.get(eye)
        if cap is None or not (0 <= index < max(1, self.frame_count)):
            return None
        key = (eye, index)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, raw = cap.read()
        if not ok:
            return None
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY) if raw.ndim == 3 else raw
        self._cache[key] = gray
        if len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return gray

    def load_pose(self) -> tuple[str, float, list[PoseFrame]] | None:
        """读取 clip_dir/pose2d.json；不存在返回 None。"""
        path = self.clip_dir / "pose2d.json"
        if not path.is_file():
            return None
        return read_pose2d(path)

    def capture_meta(self) -> dict:
        path = self.clip_dir / "capture_meta.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def close(self) -> None:
        for cap in self._caps.values():
            cap.release()
        self._caps.clear()


class PlayerWidget(FrameView):
    """回放画面：骨架叠加（33 点 + 连线，置信度着色）+ 关键点拖动修正。"""

    # 关键点拖拽落点（归一化坐标），由审核页调核心 PoseFrame.correct 落账
    keypoint_moved = Signal(int, float, float)  # (关键点索引, x, y)

    HIT_RADIUS_PX = 10  # 命中半径（控件像素）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pose: PoseFrame | None = None
        self._show_pose = True
        self._simplify_hands = False  # 简化手部：隐藏细节点（仅手腕可编辑）
        self._drag_kp: int | None = None
        self._drag_pos: tuple[float, float] | None = None  # 拖拽中的归一化坐标
        self._hover_kp: int | None = None  # 悬停高亮的关键点（显示中文关节名）
        self.setMouseTracking(True)  # 悬停识别关节（不按下也跟踪）

    def set_frame_data(self, frame: np.ndarray | None, pose: PoseFrame | None = None) -> None:
        self.set_image(gray_to_qimage(frame) if frame is not None else None)
        self._pose = pose
        self._drag_kp = None
        self._drag_pos = None
        self._hover_kp = None
        self.update()

    def set_pose_visible(self, visible: bool) -> None:
        self._show_pose = visible
        self.update()

    def set_simplify_hands(self, on: bool) -> None:
        """简化手部：隐藏 pinky/index/thumb 细节点（数据仍保留 33 点全量）。"""
        self._simplify_hands = on
        if on and self._hover_kp in HAND_DETAIL_INDICES:
            self._hover_kp = None
        self.update()

    def _hidden(self, idx: int) -> bool:
        return self._simplify_hands and idx in HAND_DETAIL_INDICES

    # ---- 拖动修正 ----

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._pose is None or self._image is None:
            return
        idx = self._hit_keypoint(event.position())
        if idx is not None:
            self._drag_kp = idx
            self._drag_pos = self._norm_at(event.position())
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_kp is not None:
            self._drag_pos = self._norm_at(event.position())
            self.update()
        elif self._pose is not None and self._show_pose and self._image is not None:
            # 悬停识别关节：最近的可见关键点高亮 + 中文名（错位修正前先看清点是谁）
            idx = self._hit_keypoint(event.position())
            if idx != self._hover_kp:
                self._hover_kp = idx
                self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_kp is not None:
            self._hover_kp = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_kp is not None:
            x, y = self._norm_at(event.position())
            self.keypoint_moved.emit(self._drag_kp, x, y)
            self._drag_kp = None
            self._drag_pos = None
            self.update()

    def _norm_at(self, pos: QPointF) -> tuple[float, float]:
        x, y = self.widget_to_image(pos)
        iw, ih = self.image_size()
        nx = min(max(x / iw, 0.0), 1.0) if iw else 0.0
        ny = min(max(y / ih, 0.0), 1.0) if ih else 0.0
        return (nx, ny)

    def _hit_keypoint(self, pos: QPointF) -> int | None:
        assert self._pose is not None
        best: int | None = None
        best_d = float(self.HIT_RADIUS_PX)
        iw, ih = self.image_size()
        for i, kp in enumerate(self._pose.keypoints):
            if self._hidden(i):
                continue
            w = self.image_to_widget(kp.x * iw, kp.y * ih)
            d = ((w.x() - pos.x()) ** 2 + (w.y() - pos.y()) ** 2) ** 0.5
            if d <= best_d:
                best = i
                best_d = d
        return best

    def _connection_color(self, a: int, b: int) -> QColor:
        """连线按身体侧着色：左橙 / 右蓝 / 中线灰（快速区分左右肢体）。"""
        if a in _LEFT_SIDE and b in _LEFT_SIDE:
            return semantic_color("orange")
        if a in _RIGHT_SIDE and b in _RIGHT_SIDE:
            return semantic_color("blue")
        return semantic_color("fg_dim")

    # ---- 叠加绘制 ----

    def paint_overlay(self, painter: QPainter) -> None:
        if self._image is None or self._pose is None or not self._show_pose:
            return
        iw, ih = self.image_size()
        kps = self._pose.keypoints

        def point_of(i: int) -> QPointF:
            if self._drag_kp == i and self._drag_pos is not None:
                return self.image_to_widget(self._drag_pos[0] * iw, self._drag_pos[1] * ih)
            return self.image_to_widget(kps[i].x * iw, kps[i].y * ih)

        # 连线（按身体侧着色：左橙 / 右蓝 / 中线灰；简化手部时隐藏细节点连线）
        for a, b in POSE_CONNECTIONS:
            if self._hidden(a) or self._hidden(b):
                continue
            painter.setPen(QPen(self._connection_color(a, b), 1))
            painter.drawLine(point_of(a), point_of(b))
        # 关键点：十字（置信度着色）+ 手动修正方框
        for i, kp in enumerate(kps):
            if self._hidden(i):
                continue
            p = point_of(i)
            color = confidence_color(kp.visibility)
            painter.setPen(QPen(color, 1))
            arm = 4.0
            painter.drawLine(QPointF(p.x() - arm, p.y()), QPointF(p.x() + arm, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - arm), QPointF(p.x(), p.y() + arm))
            if kp.manual:
                painter.drawRect(int(p.x() - 5), int(p.y() - 5), 10, 10)
        # 悬停高亮：白色实线圈 + 中文关节名标签（识别点对应的关节）
        if self._hover_kp is not None and self._drag_kp is None:
            self._draw_kp_highlight(painter, point_of(self._hover_kp), self._hover_kp, dashed=False)
        # 拖拽中的点高亮圈 + 中文关节名
        if self._drag_kp is not None and self._drag_pos is not None:
            self._draw_kp_highlight(painter, point_of(self._drag_kp), self._drag_kp, dashed=True)

    def _draw_kp_highlight(self, painter: QPainter, p: QPointF, idx: int, dashed: bool) -> None:
        painter.setPen(QPen(QColor(255, 255, 255), 1.5,
                            Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(p, 8, 8)
        label = KEYPOINT_LABELS_CN[idx]
        painter.setFont(ui_font(12, bold=True))
        metrics = painter.fontMetrics()
        w = metrics.horizontalAdvance(label)
        h = metrics.height()
        # 标签默认右上，贴近右/上边缘时翻转到左下，避免画出控件外
        lx = p.x() + 12 if p.x() + 12 + w + 12 <= self.width() else p.x() - 12 - w
        ly = p.y() - 12 if p.y() - 12 - h >= 0 else p.y() + 12 + h
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawRoundedRect(QRectF(lx - 5, ly - h + 2, w + 10, h + 4), 5, 5)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRectF(lx, ly - h + 2, w, h), Qt.AlignmentFlag.AlignCenter, label)
