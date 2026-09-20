"""环境合规 8 项检查（PRD F11）：全部为接收帧序列/参数的纯函数，阈值全部参数化。

无 Qt 依赖，可无头测试。坐标约定：roi/bbox 均为 (x, y, w, h) 同一坐标系。
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from app.envcheck.defaults import MAINS_FREQS_HZ
from app.envcheck.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    CheckResult,
)

Roi = tuple[int, int, int, int]
BBox = tuple[float, float, float, float]


def _result(check_id: str, name: str, status: str, measured: str, suggestion: str = "") -> CheckResult:
    return CheckResult(check_id=check_id, name=name, status=status, measured=measured, suggestion=suggestion)


# ---- 1 光照充足性 ----


def check_brightness(
    roi_means: Sequence[float],
    highlight_ratios: Sequence[float] | None = None,
    *,
    fail_below: float = 60.0,
    warn_below: float = 90.0,
    high: float = 200.0,
    highlight_ratio: float = 0.05,
) -> CheckResult:
    """ROI 灰度均值：<60 fail；60–90 warn；90–200 pass；>200 且高光(≥250)占比>5% warn。"""
    name = "光照充足性"
    if len(roi_means) == 0:
        return _result("brightness", name, STATUS_SKIP, "无帧数据")
    mean = float(np.mean(roi_means))
    measured = f"ROI 灰度均值 {mean:.0f}"
    if mean < fail_below:
        return _result("brightness", name, STATUS_FAIL, measured,
                       "照度严重不足，请增加补光（目标：固定曝光下画面明亮不噪）")
    if mean < warn_below:
        return _result("brightness", name, STATUS_WARN, measured, "画面偏暗，建议补光")
    if mean > high and highlight_ratios is not None and len(highlight_ratios) > 0:
        hl = float(np.mean(highlight_ratios))
        if hl > highlight_ratio:
            return _result("brightness", name, STATUS_WARN,
                           f"{measured}，高光占比 {hl * 100:.1f}%",
                           "局部过曝，收光圈或降增益")
    return _result("brightness", name, STATUS_PASS, measured)


# ---- 2 频闪 ----


def _detect_mains_flicker(means: np.ndarray, fps: float, tol_hz: float) -> float | None:
    """FFT 主频检测：主频落在 100/120Hz±tol 且显著强于带外分量 → 返回该频率。"""
    if means.size < 8 or fps <= 0:
        return None
    centered = means - means.mean()
    spec = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(means.size, d=1.0 / fps)
    spec[0] = 0.0  # 去直流
    for f0 in MAINS_FREQS_HZ:
        if f0 >= fps / 2:
            continue
        band = (freqs >= f0 - tol_hz) & (freqs <= f0 + tol_hz)
        if not band.any():
            continue
        peak = float(spec[band].max())
        outside = float(spec[~band].max()) if (~band).any() else 0.0
        if peak > 0 and peak >= 1.5 * max(outside, 1e-9):
            return f0
    return None


def check_flicker(
    frame_means: Sequence[float],
    fps: float,
    *,
    warn_pct: float = 2.0,
    fail_pct: float = 5.0,
    mains_tol_hz: float = 6.0,
) -> CheckResult:
    """帧均值序列 (max-min)/mean：<2% pass；2–5% warn；>5% fail。

    附 FFT 主频检测：主频在 100/120Hz 附近且分量强 → 建议中点名「市电频闪特征」。
    """
    name = "照明频闪"
    arr = np.asarray(frame_means, dtype=np.float64)
    if arr.size < 2:
        return _result("flicker", name, STATUS_SKIP, "帧数不足")
    mean = float(arr.mean())
    if mean <= 0:
        return _result("flicker", name, STATUS_SKIP, "画面全黑，无法判定")
    variation = float(arr.max() - arr.min()) / mean * 100.0
    mains = _detect_mains_flicker(arr, fps, mains_tol_hz)
    mains_note = f"；检测到 {mains:.0f}Hz 分量，为市电频闪特征" if mains else ""
    measured = f"帧均值波动 {variation:.2f}%"
    if variation > fail_pct:
        return _result("flicker", name, STATUS_FAIL, measured,
                       f"照明频闪超标，换恒流无频闪 LED{mains_note}")
    if variation > warn_pct:
        return _result("flicker", name, STATUS_WARN, measured,
                       f"存在轻微频闪，建议使用恒流无频闪 LED 补光{mains_note}")
    return _result("flicker", name, STATUS_PASS, measured)


# ---- 3 清晰度 ----


def check_sharpness(
    roi_frames: Sequence[np.ndarray],
    *,
    warn_below: float = 30.0,
) -> CheckResult:
    """ROI Laplacian 方差均值，低于阈值 → warn（画面偏模糊）。"""
    name = "画面清晰度"
    frames = [f for f in roi_frames if f is not None and f.size > 0]
    if not frames:
        return _result("sharpness", name, STATUS_SKIP, "无帧数据")
    var = float(np.mean([cv2.Laplacian(f, cv2.CV_64F).var() for f in frames]))
    measured = f"Laplacian 方差 {var:.1f}"
    if var < warn_below:
        return _result("sharpness", name, STATUS_WARN, measured, "画面偏模糊，检查对焦/镜头")
    return _result("sharpness", name, STATUS_PASS, measured)


# ---- 4 背景干扰 ----


def check_background_motion(
    frames: Sequence[np.ndarray],
    roi: Roi,
    *,
    thresh: float = 4.0,
) -> CheckResult:
    """ROI 外区域帧差能量均值超阈 → warn（背景有持续运动）。"""
    name = "背景干扰"
    if len(frames) < 2:
        return _result("bg_motion", name, STATUS_SKIP, "帧数不足")
    x, y, w, h = roi
    energies: list[float] = []
    for prev, curr in zip(frames, frames[1:]):
        diff = cv2.absdiff(prev, curr)
        mask = np.ones(diff.shape, dtype=bool)
        mask[y : y + h, x : x + w] = False
        if not mask.any():
            break
        energies.append(float(diff[mask].mean()))
    if not energies:
        return _result("bg_motion", name, STATUS_SKIP, "ROI 外无有效区域")
    energy = float(np.mean(energies))
    measured = f"ROI 外帧差能量 {energy:.2f}"
    if energy > thresh:
        return _result("bg_motion", name, STATUS_WARN, measured,
                       "背景有持续运动（可能有人走动），素材背景稳定性差")
    return _result("bg_motion", name, STATUS_PASS, measured)


# ---- 5 相机水平 ----


def _line_angles(frame: np.ndarray) -> list[tuple[float, float]]:
    """Canny+Hough 提取近水平线段，返回 (角度°, 长度) 列表。"""
    edges = cv2.Canny(frame, 50, 150)
    min_len = max(20, frame.shape[1] // 4)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60,
                            minLineLength=min_len, maxLineGap=10)
    if lines is None:
        return []
    out: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        dx, dy = float(x2 - x1), float(y2 - y1)
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx))
        # 归一化到 (-90, 90]
        while angle > 90.0:
            angle -= 180.0
        while angle <= -90.0:
            angle += 180.0
        if abs(angle) <= 45.0:  # 只统计近水平线
            out.append((angle, length))
    return out


def check_level(
    frames: Sequence[np.ndarray],
    *,
    warn_deg: float = 2.0,
    fail_deg: float = 5.0,
    min_lines: int = 3,
) -> CheckResult:
    """场景强直线估计主水平线倾角：>2° warn、>5° fail；直线不足 → skip。"""
    name = "相机水平"
    angles: list[float] = []
    weights: list[float] = []
    for frame in list(frames)[:5]:
        for angle, length in _line_angles(frame):
            angles.append(angle)
            weights.append(length)
    if len(angles) < min_lines:
        return _result("level", name, STATUS_SKIP,
                       f"仅检测到 {len(angles)} 条直线", "场景中直线不足，无法判定水平")
    tilt = float(np.average(angles, weights=weights))
    measured = f"主水平线倾角 {tilt:+.2f}°"
    if abs(tilt) > fail_deg:
        return _result("level", name, STATUS_FAIL, measured,
                       "相机条未水平（双目要求基线水平，请调平）")
    if abs(tilt) > warn_deg:
        return _result("level", name, STATUS_WARN, measured,
                       "相机条略有倾斜（双目要求基线水平，建议调平）")
    return _result("level", name, STATUS_PASS, measured)


# ---- 6 构图覆盖 ----


def check_framing(
    person_bboxes: Sequence[BBox | None],
    roi: Roi,
    *,
    tight_margin: float = 0.05,
    min_height_ratio: float = 0.40,
) -> CheckResult:
    """人形 bbox 序列（中位数聚合）与 ROI 的覆盖关系。

    人形超出 ROI → fail；头顶/脚底距 ROI 边缘 <5% → warn（构图过紧）；
    人形高度 <40% ROI 高度 → warn（距离过远，建议 2–3.5m）。
    """
    name = "构图覆盖"
    boxes = [b for b in person_bboxes if b is not None]
    if not boxes:
        return _result("framing", name, STATUS_SKIP, "体检期间未检测到人体",
                       "可让人站入打击区后重测")
    arr = np.asarray(boxes, dtype=np.float64)
    bx, by, bw, bh = np.median(arr, axis=0)
    rx, ry, rw, rh = roi
    measured = f"人形高度占 ROI {bh / rh * 100:.0f}%"
    if bx < rx or by < ry or bx + bw > rx + rw or by + bh > ry + rh:
        return _result("framing", name, STATUS_FAIL, measured, "打击区框选未覆盖人体")
    suggestions: list[str] = []
    top_gap = (by - ry) / rh
    bottom_gap = (ry + rh - (by + bh)) / rh
    if top_gap < tight_margin or bottom_gap < tight_margin:
        suggestions.append("构图过紧（头顶/脚底距边缘过近），请后退或调整取景")
    if bh < min_height_ratio * rh:
        suggestions.append("人形占比过小，距离过远（建议 2–3.5m）")
    if suggestions:
        return _result("framing", name, STATUS_WARN, measured, "；".join(suggestions))
    return _result("framing", name, STATUS_PASS, measured)


# ---- 7 帧率/掉帧 ----


def check_framerate(
    timestamps_ns: Sequence[int],
    nominal_fps: float,
    *,
    gap_factor: float = 1.5,
    warn_rate: float = 0.001,
    fail_rate: float = 0.01,
) -> CheckResult:
    """实测 fps 与掉帧率（帧间隔 >1.5×标称帧间隔计为掉帧，与核心层口径一致）。

    掉帧率 ≥1% → fail；≥0.1% → warn。
    """
    name = "帧率/掉帧"
    ts = np.asarray(timestamps_ns, dtype=np.int64)
    if ts.size < 2 or nominal_fps <= 0:
        return _result("framerate", name, STATUS_SKIP, "帧数不足")
    intervals = np.diff(ts)
    nominal_ns = 1e9 / nominal_fps
    dropped = int(np.count_nonzero(intervals > gap_factor * nominal_ns))
    rate = dropped / float(intervals.size)
    span_s = (int(ts[-1]) - int(ts[0])) / 1e9
    measured_fps = (ts.size - 1) / span_s if span_s > 0 else 0.0
    measured = f"实测 {measured_fps:.1f}fps，掉帧率 {rate * 100:.2f}%"
    if rate >= fail_rate:
        return _result("framerate", name, STATUS_FAIL, measured,
                       "掉帧超标（检查 USB 带宽/存储写入/采集分辨率）")
    if rate >= warn_rate:
        return _result("framerate", name, STATUS_WARN, measured,
                       "存在少量掉帧，建议排查 USB 带宽与存储写入")
    return _result("framerate", name, STATUS_PASS, measured)


# ---- 8 磁盘空间（独立于帧检查） ----


def check_disk(
    path: str | Path,
    planned_clips: int = 200,
    est_mb_per_clip: float = 500.0,
    *,
    warn_factor: float = 2.0,
) -> CheckResult:
    """磁盘余量：不足计划用量 → fail；仅够 2 倍以内 → warn。"""
    name = "磁盘空间"
    # 存储根目录可能尚未创建：向上找最近的已存在父目录再测余量
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_mb = shutil.disk_usage(probe).free / 1e6
    except OSError as e:
        return _result("disk", name, STATUS_SKIP, f"无法读取磁盘信息: {e}")
    need_mb = planned_clips * est_mb_per_clip
    measured = f"余量 {free_mb / 1000:.1f}GB，计划 {planned_clips} 段约需 {need_mb / 1000:.1f}GB"
    if free_mb < need_mb:
        return _result("disk", name, STATUS_FAIL, measured,
                       "磁盘空间不足，请清理后再采集")
    if free_mb < warn_factor * need_mb:
        enough = int(free_mb / max(est_mb_per_clip, 1e-9))
        return _result("disk", name, STATUS_WARN, measured,
                       f"磁盘余量仅够约 {enough} 段，建议清理磁盘")
    return _result("disk", name, STATUS_PASS, measured)
