"""ROI 挥棒检测：帧间运动能量阈值触发 + 回落结束。"""

from __future__ import annotations

import cv2
import numpy as np

from app.detect.presence import Roi, crop_roi


class SwingDetector:
    """挥棒事件检测。

    ROI 内帧间平均绝对差（运动能量）超过 trigger_thresh 触发；
    能量回落到 release_thresh 以下且持续 post_roll_seconds 判定结束。
    片段区间由状态机按 trigger_idx ± pre_roll/post_roll 换算。
    """

    def __init__(
        self,
        roi: Roi,
        fps: float,
        trigger_thresh: float = 15.0,
        release_thresh: float = 8.0,
        pre_roll_seconds: float = 1.0,
        post_roll_seconds: float = 1.0,
    ) -> None:
        self.roi = roi
        self.fps = float(fps)
        self.trigger_thresh = float(trigger_thresh)
        self.release_thresh = float(release_thresh)
        self.pre_roll_frames = max(0, int(pre_roll_seconds * fps + 0.5))
        self.post_roll_frames = max(1, int(post_roll_seconds * fps + 0.5))
        self._prev: np.ndarray | None = None
        self._active = False
        self._trigger_idx = -1
        self._quiet = 0
        self.last_energy = 0.0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def trigger_idx(self) -> int:
        return self._trigger_idx

    def energy(self, frame_gray: np.ndarray) -> float:
        roi_frame = crop_roi(frame_gray, self.roi)
        if self._prev is None:
            self._prev = roi_frame
            return 0.0
        diff = cv2.absdiff(roi_frame, self._prev)
        self._prev = roi_frame
        return float(diff.mean())

    def update(self, frame_gray: np.ndarray, frame_idx: int) -> str | None:
        """喂入一帧，返回 "started" / "ended" / None。"""
        e = self.energy(frame_gray)
        self.last_energy = e
        if not self._active:
            if e > self.trigger_thresh:
                self._active = True
                self._trigger_idx = frame_idx
                self._quiet = 0
                return "started"
            return None
        # 挥棒进行中：能量回落且持续 post_roll 帧判结束
        if e < self.release_thresh:
            self._quiet += 1
            if self._quiet >= self.post_roll_frames:
                self._active = False
                return "ended"
        else:
            self._quiet = 0
        return None

    def reset(self) -> None:
        self._prev = None
        self._active = False
        self._trigger_idx = -1
        self._quiet = 0
        self.last_energy = 0.0
