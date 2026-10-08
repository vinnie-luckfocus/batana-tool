"""ROI 挥棒检测：显著运动像素占比阈值触发 + 回落结束。"""

from __future__ import annotations

import cv2
import numpy as np

from app.detect.presence import Roi, crop_roi


class SwingDetector:
    """挥棒事件检测。

    判据 = **显著运动像素占比**（motion ratio）：ROI 内帧间差分超过
    pix_thresh 的像素数 / ROI 总像素数。

    为什么不用平均帧差（mean abs diff）：它对运动覆盖面积极度敏感——
    真实挥棒中手臂+球棒只占 ROI 的 2–10%，平均到全 ROI 后能量只有 1–5
    （0–255 量纲），与传感器底噪（~1–3）拉不开差距，阈值没法定。
    占比指标先按像素阈值滤掉底噪再计数，静止底噪 ≈0%，挥棒 ≈2–20%，
    区分度两个数量级。

    占比超过 trigger_ratio 触发；回落到 release_ratio 以下且持续
    post_roll_seconds 判定结束。片段区间由状态机按 trigger_idx ±
    pre_roll/post_roll 换算。
    """

    def __init__(
        self,
        roi: Roi,
        fps: float,
        pix_thresh: float = 25.0,
        trigger_ratio: float = 0.02,
        release_ratio: float = 0.008,
        pre_roll_seconds: float = 1.0,
        post_roll_seconds: float = 1.0,
    ) -> None:
        self.roi = roi
        self.fps = float(fps)
        self.pix_thresh = float(pix_thresh)
        self.trigger_ratio = float(trigger_ratio)
        self.release_ratio = float(release_ratio)
        self.pre_roll_frames = max(0, int(pre_roll_seconds * fps + 0.5))
        self.post_roll_frames = max(1, int(post_roll_seconds * fps + 0.5))
        self._prev: np.ndarray | None = None
        self._active = False
        self._trigger_idx = -1
        self._quiet = 0
        self.last_ratio = 0.0   # 显著运动像素占比（0–1），触发判据
        self.last_energy = 0.0  # 平均帧差（0–255），仅遥测参考

    @property
    def active(self) -> bool:
        return self._active

    @property
    def trigger_idx(self) -> int:
        return self._trigger_idx

    def _metrics(self, frame_gray: np.ndarray) -> tuple[float, float]:
        """返回 (motion_ratio, mean_energy)。"""
        roi_frame = crop_roi(frame_gray, self.roi)
        if self._prev is None:
            self._prev = roi_frame
            return 0.0, 0.0
        diff = cv2.absdiff(roi_frame, self._prev)
        self._prev = roi_frame
        ratio = float(np.count_nonzero(diff > self.pix_thresh)) / float(diff.size)
        return ratio, float(diff.mean())

    def update(self, frame_gray: np.ndarray, frame_idx: int) -> str | None:
        """喂入一帧，返回 "started" / "ended" / None。"""
        ratio, energy = self._metrics(frame_gray)
        self.last_ratio = ratio
        self.last_energy = energy
        if not self._active:
            if ratio > self.trigger_ratio:
                self._active = True
                self._trigger_idx = frame_idx
                self._quiet = 0
                return "started"
            return None
        # 挥棒进行中：运动占比回落且持续 post_roll 帧判结束
        if ratio < self.release_ratio:
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
        self.last_ratio = 0.0
        self.last_energy = 0.0
