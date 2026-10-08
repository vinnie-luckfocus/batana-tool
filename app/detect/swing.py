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
        # 触发确认：连续 25ms 超阈帧数（120fps=3 帧）。倒计时结束的收势/调节姿态
        # 是 1-2 帧毛刺，真挥棒 2%+ 会持续数十帧（实机误触发取证后的修复）
        self.sustain_needed = max(1, int(0.025 * fps + 0.5))
        self._sustain = 0
        self._sustain_start = -1
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
                # 持续帧确认：毛刺不触发；触发帧记为连续段起点，
                # pre_roll 仍覆盖运动起点之前
                if self._sustain == 0:
                    self._sustain_start = frame_idx
                self._sustain += 1
                if self._sustain >= self.sustain_needed:
                    self._active = True
                    self._trigger_idx = self._sustain_start
                    self._quiet = 0
                    self._sustain = 0
                    return "started"
            else:
                self._sustain = 0
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
        self._sustain = 0
        self._sustain_start = -1
        self.last_ratio = 0.0
        self.last_energy = 0.0
