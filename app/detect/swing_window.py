"""挥棒段自动截取建议：触发帧锚定 + 腕肘速度精修（无 Qt 依赖，可无头单测）。

实证依据（2026-10 真实素材分析）：腕/肘速度主簇骑跨采集触发帧，
触发前 ~150–225ms 的小速度簇是引棒段，因此基础窗口取
[trigger − 150ms, trigger + 400ms]（120fps 约 66 帧），精修只在此附近微调。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 双肘双腕（BlazePose 下标）：速度估计取每帧可见性最高者
_SPEED_KEYPOINTS = (13, 14, 15, 16)

# 基础窗口（触发帧锚定）
_PRE_TRIGGER_S = 0.150
_POST_TRIGGER_S = 0.400
# 无触发帧时的退化峰值窗口
_PEAK_PRE_S = 0.35
_PEAK_POST_S = 0.15
# 精修搜索范围（相对基础窗口的扩展，秒）
_REFINE_BACK_S = 0.15
_REFINE_FWD_S = 0.20
# 静默边界阈值 = 峰值速度 × 该比例
_QUIET_RATIO = 0.12


@dataclass
class SwingWindow:
    """建议窗口（闭区间帧号）+ 来源说明（用于状态栏/报告）。"""

    start: int
    end: int
    source: str

    def __iter__(self):
        # 允许 start, end = suggest_swing_window(...) 解包
        yield self.start
        yield self.end


def _clamp(v: int, frame_count: int) -> int:
    return int(np.clip(v, 0, frame_count - 1))


def wrist_elbow_speed(
    pose_frames: list,
    resolution: tuple[int, int],
    fps: float,
) -> np.ndarray:
    """每帧速度（px/s）：双腕双肘中当前帧 visibility 最高者的帧间像素位移。

    返回长度与 pose_frames 相同（首帧为 0）；随后由调用方做 5 帧中值平滑。
    """
    w, h = resolution
    n = len(pose_frames)
    speed = np.zeros(n)
    for i in range(1, n):
        cur, prev = pose_frames[i], pose_frames[i - 1]
        best_vis, best_speed = -1.0, 0.0
        for kp in _SPEED_KEYPOINTS:
            a, b = prev.keypoints[kp], cur.keypoints[kp]
            if min(a.visibility, b.visibility) <= 0.0:
                continue  # 该点不可见：不参与速度估计
            vis = cur.keypoints[kp].visibility
            s = float(np.hypot((b.x - a.x) * w, (b.y - a.y) * h)) * fps
            # 可见性最高者优先；可见性相同（常见）时取速度最大者（挥棒主簇更强）
            if (vis, s) > (best_vis, best_speed):
                best_vis, best_speed = vis, s
        speed[i] = best_speed
    return speed


def _median5(values: np.ndarray) -> np.ndarray:
    """5 帧中值平滑（边界截断）。"""
    n = len(values)
    out = values.copy()
    for i in range(n):
        out[i] = np.median(values[max(0, i - 2): i + 3])
    return out


def _refine_window(
    base_start: int,
    base_end: int,
    speed: np.ndarray,
    fps: float,
    frame_count: int,
) -> tuple[int, int] | None:
    """基础窗口附近找速度静默边界：向前找挥棒起点、向后找峰后回落点。

    精修失败（无有效峰/边界反常）返回 None，由调用方回退基础窗口。
    """
    n = len(speed)
    if n < 3:
        return None
    sm = _median5(speed)
    lo = max(0, int(base_start - _REFINE_BACK_S * fps))
    hi = min(n - 1, int(base_end + _REFINE_FWD_S * fps))
    if lo >= hi:
        return None
    # 峰在基础窗口内取（挥棒主簇骑跨触发帧）
    seg = sm[max(0, base_start): min(n, base_end + 1)]
    if seg.size == 0 or float(seg.max()) <= 0.0:
        return None
    peak = int(np.argmax(seg)) + max(0, base_start)
    quiet = float(sm[peak]) * _QUIET_RATIO

    # 起点：从基础起点向前找最后一个静默帧，其下一帧为挥棒起点
    start = base_start
    for i in range(min(base_start, peak), lo - 1, -1):
        if sm[i] < quiet:
            start = i + 1
            break
    else:
        start = lo  # 搜索区内始终高于阈值：取搜索下界（不过度回溯）

    # 终点：从峰后向前找回落到静默的第一帧；找不到则回退基础终点
    end = base_end
    for i in range(peak + 1, hi + 1):
        if sm[i] < quiet:
            end = i
            break
    if start >= end:
        return None
    return _clamp(start, frame_count), _clamp(end, frame_count)


def suggest_swing_window(
    frame_count: int,
    fps: float,
    trigger_idx: int | None = None,
    pose_frames: list | None = None,
    resolution: tuple[int, int] | None = None,
) -> SwingWindow:
    """建议挥棒段窗口（闭区间）。返回 SwingWindow(start, end, source)。

    优先级：
    1. 有 trigger_idx：基础窗口 [trig−150ms, trig+400ms]；有骨架+分辨率时
       用腕肘速度静默边界精修，精修失败回退基础窗口；
    2. 无 trigger_idx 但有骨架+分辨率：速度峰值法（峰前 350ms ~ 峰后 150ms）；
    3. 两者皆无：整段。
    """
    if frame_count <= 0:
        raise ValueError(f"frame_count 须为正数: {frame_count}")
    speed: np.ndarray | None = None
    if pose_frames is not None and resolution is not None and len(pose_frames) >= 3:
        speed = wrist_elbow_speed(pose_frames, resolution, fps)

    if trigger_idx is not None:
        base_start = _clamp(round(trigger_idx - _PRE_TRIGGER_S * fps), frame_count)
        base_end = _clamp(round(trigger_idx + _POST_TRIGGER_S * fps), frame_count)
        if speed is not None:
            refined = _refine_window(base_start, base_end, speed, fps, frame_count)
            if refined is not None:
                return SwingWindow(refined[0], refined[1], "触发帧锚定 + 腕肘速度精修")
        return SwingWindow(base_start, base_end, "触发帧锚定（-150ms / +400ms）")

    if speed is not None:
        sm = _median5(speed)
        if float(sm.max()) > 0.0:
            peak = int(np.argmax(sm))
            start = _clamp(round(peak - _PEAK_PRE_S * fps), frame_count)
            end = _clamp(round(peak + _PEAK_POST_S * fps), frame_count)
            if start < end:
                return SwingWindow(start, end, "速度峰值（无触发帧，峰前 350ms / 峰后 150ms）")

    return SwingWindow(0, frame_count - 1, "整段（无触发帧与可用骨架速度）")
