"""环境体检执行器（PRD F11）：从帧源采 duration_s 秒帧，依次跑 8 项检查。

帧源复用核心层 FrameSource 抽象（UvcSource/FileSource 均可），无 Qt 依赖。
为控制内存与耗时，逐帧只保留 ROI/整帧灰度均值与高光占比；帧图像按约 12 张/秒
降采样并半分辨率缓存（≤120 张），供清晰度/背景/水平/构图检查使用——除清晰度外
各检查均为比例/角度量纲，缩放不影响结论（清晰度阈值按半分辨率口径标定）。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from app.capture import FrameSource, split_sbs
from app.detect import Roi
from app.envcheck import checks
from app.envcheck.defaults import EnvCheckSettings
from app.envcheck.models import EnvironmentReport

ProgressCb = Callable[[int, int, str], None]  # (已完成项数, 总项数, 当前检查中文名)

_TOTAL_CHECKS = 8
_MAX_CACHED_FRAMES = 120
_ANALYSIS_FPS = 12.0  # 缓存帧目标采样率
_DOWNSCALE = 2        # 缓存帧降采样倍数


def _person_bboxes(frames: list[np.ndarray], roi: Roi) -> list[tuple[float, float, float, float] | None]:
    """MOG2 前景最大连通域 → 人形 bbox 序列（与 frames 同坐标系）。

    慢学习率（0.001）让静止人形长时间保持前景；无显著连通域的帧返回 None。
    """
    x, y, w, h = roi
    bg = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=25, detectShadows=False)
    kernel = np.ones((3, 3), np.uint8)
    boxes: list[tuple[float, float, float, float] | None] = []
    for frame in frames:
        crop = frame[y : y + h, x : x + w]
        fg = bg.apply(crop, learningRate=0.001)
        mask = (fg == 255).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            boxes.append(None)
            continue
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) < 0.02 * crop.size:
            boxes.append(None)
            continue
        bx, by, bw, bh = cv2.boundingRect(largest)
        boxes.append((float(x + bx), float(y + by), float(bw), float(bh)))
    return boxes


class EnvironmentChecker:
    """环境体检编排：采样 → 8 项检查 → 报告（可选落盘 env_reports/）。"""

    def __init__(
        self,
        frame_source: FrameSource,
        roi: Roi | None = None,
        settings: EnvCheckSettings | None = None,
        report_root: str | Path | None = None,
    ) -> None:
        self.source = frame_source
        self.roi = roi  # 左目像素坐标；None → 首帧按画面尺寸建默认中央 ROI
        self.cfg = settings or EnvCheckSettings()
        self.report_root = Path(report_root) if report_root else None
        self._cancelled = False

    def cancel(self) -> None:
        """请求中止采样（UI 关闭对话框时调用）。"""
        self._cancelled = True

    def run(self, duration_s: float = 10.0, progress_cb: ProgressCb | None = None) -> EnvironmentReport:
        """采 duration_s 秒帧并逐项检查，返回报告；report_root 非空时落盘 JSON。"""
        t0 = time.monotonic()
        roi_means, frame_means, highlights, ts_list = self._sample(duration_s)

        cfg = self.cfg
        fps = float(self.source.fps)
        # 缓存帧坐标系（半分辨率）下的 ROI
        roi = self._active_roi
        sroi = (roi[0] // _DOWNSCALE, roi[1] // _DOWNSCALE,
                max(1, roi[2] // _DOWNSCALE), max(1, roi[3] // _DOWNSCALE))
        sx, sy, sw, sh = sroi
        roi_frames = [f[sy : sy + sh, sx : sx + sw] for f in self._cached]

        report = EnvironmentReport(
            duration_s=(ts_list[-1] - ts_list[0]) / 1e9 if len(ts_list) > 1 else 0.0,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

        plan = [
            lambda: checks.check_brightness(
                roi_means, highlights,
                fail_below=cfg.brightness_fail, warn_below=cfg.brightness_warn,
                high=cfg.brightness_high, highlight_ratio=cfg.highlight_ratio,
            ),
            lambda: checks.check_flicker(
                frame_means, fps,
                warn_pct=cfg.flicker_warn_pct, fail_pct=cfg.flicker_fail_pct,
                mains_tol_hz=cfg.mains_tol_hz,
            ),
            lambda: checks.check_sharpness(roi_frames, warn_below=cfg.sharpness_warn),
            lambda: checks.check_background_motion(
                self._cached, sroi, thresh=cfg.bg_motion_thresh),
            lambda: checks.check_level(
                self._cached, warn_deg=cfg.level_warn_deg,
                fail_deg=cfg.level_fail_deg, min_lines=cfg.level_min_lines),
            lambda: checks.check_framing(
                _person_bboxes(self._cached, sroi), sroi,
                tight_margin=cfg.framing_tight_margin,
                min_height_ratio=cfg.framing_min_height_ratio),
            lambda: checks.check_framerate(
                ts_list, fps, gap_factor=cfg.drop_gap_factor,
                warn_rate=cfg.drop_warn_rate, fail_rate=cfg.drop_fail_rate),
            lambda: checks.check_disk(
                self.report_root or Path.cwd(), cfg.planned_clips, cfg.est_mb_per_clip,
                warn_factor=cfg.disk_warn_factor),
        ]
        for i, fn in enumerate(plan):
            result = fn()
            report.results.append(result)
            if progress_cb is not None:
                progress_cb(i + 1, _TOTAL_CHECKS, result.name)

        report.elapsed_s = time.monotonic() - t0
        if self.report_root is not None:
            report.save(self.report_root)
        return report

    # ---- 采样 ----

    def _sample(self, duration_s: float) -> tuple[list[float], list[float], list[float], list[int]]:
        """逐帧采样：ROI/整帧均值、高光占比、时间戳 + 降采样缓存帧。"""
        self._cancelled = False
        self._cached: list[np.ndarray] = []
        self._active_roi: Roi | None = None
        roi_means: list[float] = []
        frame_means: list[float] = []
        highlights: list[float] = []
        ts_list: list[int] = []
        fps = float(self.source.fps)
        stride = max(1, round(fps / _ANALYSIS_FPS))
        n_target = int(fps * duration_s) + 8
        t0 = time.monotonic()
        time_guard = duration_s * 1.5 + 5.0  # 文件源慢读/相机掉帧兜底
        for idx, ts_ns, sbs in self.source.frames():
            if self._cancelled or idx >= n_target or (time.monotonic() - t0) > time_guard:
                break
            left, _right = split_sbs(sbs)
            if self._active_roi is None:
                h, w = left.shape[:2]
                self._active_roi = self.roi or (w // 4, h // 8, w // 2, h * 3 // 4)
            x, y, w, h = self._active_roi
            crop = left[y : y + h, x : x + w]
            roi_means.append(float(crop.mean()))
            frame_means.append(float(left.mean()))
            highlights.append(float(np.count_nonzero(crop >= self.cfg.highlight_level)) / crop.size)
            ts_list.append(ts_ns)
            if idx % stride == 0 and len(self._cached) < _MAX_CACHED_FRAMES:
                small = cv2.resize(
                    left,
                    (max(1, left.shape[1] // _DOWNSCALE), max(1, left.shape[0] // _DOWNSCALE)),
                    interpolation=cv2.INTER_AREA,
                )
                self._cached.append(small)
        return roi_means, frame_means, highlights, ts_list
