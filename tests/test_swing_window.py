"""挥棒段窗口建议测试：触发帧锚定、clamp、速度精修、无触发退化。"""

from __future__ import annotations

import numpy as np
import pytest

from app.detect.swing_window import suggest_swing_window, wrist_elbow_speed
from app.pose.model import KEYPOINT_NAMES, Keypoint, PoseFrame

FPS = 120.0
RES = (640, 400)


def _frames_with_wrist_motion(n: int, moving: set[int]) -> list[PoseFrame]:
    """合成骨架序列：moving 帧腕部大位移，其余静止（visibility 全 0.9）。"""
    frames = []
    x = 0.5
    for i in range(n):
        if i in moving:
            x += 0.05
        kps = [Keypoint(name=name, x=x if k == 15 else 0.5, y=0.5, visibility=0.9)
               for k, name in enumerate(KEYPOINT_NAMES)]
        frames.append(PoseFrame(frame_index=i, timestamp_ms=i / FPS * 1000.0,
                                confidence=0.9, keypoints=kps))
    return frames


def test_trigger_anchor_base_window():
    """触发帧锚定：120fps 下 [trig−18, trig+48]（-150ms/+400ms）。"""
    win = suggest_swing_window(480, FPS, trigger_idx=120)
    assert (win.start, win.end) == (102, 168)
    assert "触发帧" in win.source
    # 可解包
    start, end = win
    assert (start, end) == (102, 168)


def test_trigger_anchor_clamps_to_clip():
    win = suggest_swing_window(100, FPS, trigger_idx=2)
    assert win.start == 0
    win = suggest_swing_window(100, FPS, trigger_idx=98)
    assert win.end == 99


def test_speed_refinement_finds_quiet_boundaries():
    """速度静默边界精修：静止→挥动→静止的合成曲线应切出挥动段。"""
    moving = set(range(100, 151))
    frames = _frames_with_wrist_motion(240, moving)
    win = suggest_swing_window(240, FPS, trigger_idx=120,
                               pose_frames=frames, resolution=RES)
    assert "精修" in win.source
    assert win.start == 100  # 挥动起点（最后一个静默帧 + 1）
    assert win.end == 151    # 峰后回落到静默的第一帧


def test_speed_refinement_fallback_to_base():
    """速度全零（无有效运动）时精修失败，回退基础窗口。"""
    frames = _frames_with_wrist_motion(240, set())
    win = suggest_swing_window(240, FPS, trigger_idx=120,
                               pose_frames=frames, resolution=RES)
    assert (win.start, win.end) == (102, 168)
    assert "精修" not in win.source


def test_no_trigger_peak_method():
    """无触发帧：速度峰值法（峰前 350ms / 峰后 150ms）。"""
    moving = set(range(128, 133))
    frames = _frames_with_wrist_motion(240, moving)
    win = suggest_swing_window(240, FPS, trigger_idx=None,
                               pose_frames=frames, resolution=RES)
    assert "峰值" in win.source
    # 中值平滑后峰在 128（平台首帧），窗口 [128−42, 128+18]
    assert (win.start, win.end) == (86, 146)


def test_no_trigger_no_pose_returns_full_range():
    win = suggest_swing_window(200, FPS, trigger_idx=None)
    assert (win.start, win.end) == (0, 199)
    assert "整段" in win.source


def test_wrist_elbow_speed_units():
    """速度为 px/s：单帧位移 0.05 × 640px × 120fps = 3840。"""
    frames = _frames_with_wrist_motion(3, {1})
    speed = wrist_elbow_speed(frames, RES, FPS)
    assert speed[0] == 0.0
    assert speed[1] == pytest.approx(0.05 * 640 * FPS, rel=1e-6)
    assert speed[2] == 0.0
