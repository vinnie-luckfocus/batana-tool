"""ROI 就位检测：背景建模（MOG2）前景占比 + 稳定帧数判定。"""

from __future__ import annotations

import cv2
import numpy as np

# ROI = (x, y, w, h)，像素坐标
Roi = tuple[int, int, int, int]


def crop_roi(frame: np.ndarray, roi: Roi) -> np.ndarray:
    x, y, w, h = roi
    return frame[y : y + h, x : x + w]


class PresenceDetector:
    """打击区就位检测。

    ROI 内前景占比超过 ratio_thresh 且连续稳定 stable_seconds 判定就位；
    就位后前景消失持续 absent_seconds 判定离场。每帧调用 update()。

    学习率策略（MOG2）：
    - 常态慢学习（learning_rate，默认 0.002）：MOG2 方差快速膨胀会把静止人形
      几帧内吸进背景，慢学习让就位静止的人长时间保持前景；
    - 空场景快学习（idle_learning_rate，默认 0.05）：上一帧占比低于阈值才启用，
      人离开后快速把空场景学进背景，避免旧人形残留干扰下次就位。
    """

    def __init__(
        self,
        roi: Roi,
        fps: float,
        ratio_thresh: float = 0.02,
        stable_seconds: float = 1.0,
        absent_seconds: float = 1.0,
        method: str = "mog2",
        learning_rate: float = 0.002,
        idle_learning_rate: float = 0.05,
    ) -> None:
        if method not in ("mog2", "framediff"):
            raise ValueError(f"未知背景建模方法: {method}")
        self.roi = roi
        self.fps = float(fps)
        self.ratio_thresh = float(ratio_thresh)
        self.stable_frames = max(1, int(stable_seconds * fps + 0.5))
        self.absent_frames = max(1, int(absent_seconds * fps + 0.5))
        self.method = method
        self.learning_rate = float(learning_rate)
        self.idle_learning_rate = float(idle_learning_rate)
        self._bg = (
            cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
            if method == "mog2"
            else None
        )
        self._prev: np.ndarray | None = None
        self._scene_empty = True  # 上一帧占比低于阈值（空场景），本帧用快学习率
        self._hit = 0
        self._miss = 0
        self._present = False
        self.last_ratio = 0.0

    @property
    def present(self) -> bool:
        return self._present

    def foreground_ratio(self, frame_gray: np.ndarray) -> float:
        """计算当前帧 ROI 前景占比。"""
        roi_frame = crop_roi(frame_gray, self.roi)
        if self.method == "mog2":
            assert self._bg is not None
            lr = self.idle_learning_rate if (self._scene_empty and not self._present) else self.learning_rate
            fg = self._bg.apply(roi_frame, learningRate=lr)
            mask = fg == 255
        else:
            if self._prev is None:
                self._prev = roi_frame
                return 0.0
            diff = cv2.absdiff(roi_frame, self._prev)
            self._prev = roi_frame
            mask = diff > 25
        return float(np.count_nonzero(mask)) / float(mask.size)

    def update(self, frame_gray: np.ndarray) -> bool:
        """喂入一帧（左目灰度整帧），返回稳定判定后的是否就位。"""
        ratio = self.foreground_ratio(frame_gray)
        self.last_ratio = ratio
        occupied = ratio > self.ratio_thresh
        self._scene_empty = not occupied
        if occupied:
            self._hit += 1
            self._miss = 0
        else:
            self._miss += 1
            self._hit = 0
        if not self._present and self._hit >= self.stable_frames:
            self._present = True
        elif self._present and self._miss >= self.absent_frames:
            self._present = False
        return self._present

    def reset(self) -> None:
        self._hit = 0
        self._miss = 0
        self._present = False
        self._scene_empty = True
        self.last_ratio = 0.0
        if self.method == "mog2":
            self._bg = cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=25, detectShadows=False
            )
        self._prev = None
