"""就位/挥棒检测器测试：合成帧序列，含阈值边界与误检用例。"""

from __future__ import annotations

import numpy as np
import pytest

from app.detect import PresenceDetector, SwingDetector

FPS = 10.0
ROI = (40, 40, 100, 100)  # 100x100 的 ROI
FRAME = (200, 240)


def empty_frame() -> np.ndarray:
    return np.zeros(FRAME, np.uint8)


def frame_with_block(x: int, y: int, w: int, h: int, value: int = 255) -> np.ndarray:
    f = empty_frame()
    f[y : y + h, x : x + w] = value
    return f


def learn_background(det: PresenceDetector, frames: int = 20) -> None:
    for _ in range(frames):
        det.update(empty_frame())


# ---- PresenceDetector ----


def test_presence_settles_after_stable_frames():
    det = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.5, absent_seconds=0.6)
    learn_background(det)
    # ROI 内放 50% 占比的块 → 前景占比 0.5 > 0.05
    occupied = frame_with_block(40, 40, 50, 100)
    for i in range(4):  # stable_frames = 0.5*10 = 5，前 4 帧不就位
        assert det.update(occupied) is False, f"第 {i+1} 帧不应就位"
    assert det.update(occupied) is True
    assert det.last_ratio == pytest.approx(0.5, abs=0.01)


def test_presence_not_settled_if_unstable():
    det = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.5)
    learn_background(det)
    occupied = frame_with_block(40, 40, 50, 100)
    # 断断续续出现，永远凑不够连续 5 帧
    for _ in range(5):
        for _ in range(4):
            det.update(occupied)
        det.update(empty_frame())
        assert det.present is False


def test_presence_ratio_boundary():
    # 占比恰低于阈值（4% < 5%）：不就位
    det = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.3)
    learn_background(det)
    below = frame_with_block(40, 40, 20, 20)  # 400/10000 = 4%
    for _ in range(10):
        assert det.update(below) is False
    # 占比高于阈值（16% > 5%）：就位
    det2 = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.3)
    learn_background(det2)
    above = frame_with_block(40, 40, 40, 40)  # 16%
    for _ in range(3):
        det2.update(above)
    assert det2.present is True


def test_presence_leaves_after_absent_timeout():
    det = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.3, absent_seconds=0.5)
    learn_background(det)
    occupied = frame_with_block(40, 40, 50, 100)
    for _ in range(3):
        det.update(occupied)
    assert det.present is True
    # 离开 4 帧（< absent_frames=5）：仍判就位
    for _ in range(4):
        assert det.update(empty_frame()) is True
    # 第 5 帧：离场
    assert det.update(empty_frame()) is False


def test_presence_framediff_method():
    det = PresenceDetector(ROI, FPS, ratio_thresh=0.05, stable_seconds=0.3, method="framediff")
    occupied = frame_with_block(40, 40, 50, 100)
    # 帧差法依赖帧间变化：交替喂空帧/占用帧，每帧 diff 占比均为 0.5
    det.update(empty_frame())   # 首帧无 prev
    det.update(occupied)        # diff 0.5，hit 1
    det.update(empty_frame())   # diff 0.5，hit 2
    assert det.update(occupied) is True  # hit 3 ≥ stable_frames → 就位


def test_presence_invalid_method():
    with pytest.raises(ValueError):
        PresenceDetector(ROI, FPS, method="unknown")


# ---- SwingDetector ----


def energy_frame(prev: np.ndarray, pixels: int, offset: int = 0) -> np.ndarray:
    """在 prev 基础上把 ROI 内 [offset, offset+pixels) 像素置 255，控制帧差能量。

    ROI 100x100：相邻帧不同区域点亮时，mean absdiff ≈ pixels * 255 / 10000。
    """
    f = prev.copy()
    sub = f[40:140, 40:140]
    sub.flat[offset : offset + pixels] = 255  # flat 赋值写回视图（ravel() 会拷贝，不能用）
    return f


def test_swing_trigger_and_end():
    det = SwingDetector(ROI, FPS, trigger_thresh=15.0, release_thresh=8.0,
                        pre_roll_seconds=1.0, post_roll_seconds=0.5)
    base = empty_frame()
    assert det.update(base, 0) is None  # 首帧无帧差
    assert det.update(base.copy(), 1) is None  # 静止
    # 能量 = 784*255/10000 ≈ 20 > 15 → 触发
    burst = energy_frame(base, 784)
    assert det.update(burst, 2) == "started"
    assert det.active is True
    assert det.trigger_idx == 2
    # 持续高能量（每帧画面都在变）：不结束（post_roll=5 帧）
    assert det.update(energy_frame(base, 392), 3) is None              # ≈10 > 8
    assert det.update(energy_frame(base, 784, offset=392), 4) is None  # 另一区域点亮
    assert det.update(energy_frame(base, 392), 5) is None
    # 画面静止不变：能量 0 < 8，持续 5 帧 → 第 5 帧结束
    still = energy_frame(base, 392)
    for i in range(4):
        assert det.update(still.copy(), 6 + i) is None
    assert det.update(still.copy(), 10) == "ended"
    assert det.active is False


def test_swing_no_false_trigger_on_small_noise():
    det = SwingDetector(ROI, FPS, trigger_thresh=15.0, release_thresh=8.0)
    base = empty_frame()
    det.update(base, 0)
    prev = base
    for i in range(1, 20):
        # 能量 = 100*255/10000 ≈ 2.55 < 15：小幅扰动不触发
        cur = energy_frame(prev, 100)
        assert det.update(cur, i) is None
        prev = cur
    assert det.active is False


def test_swing_energy_threshold_boundary():
    det = SwingDetector(ROI, FPS, trigger_thresh=10.0, release_thresh=1.0)
    base = empty_frame()
    det.update(base, 0)
    # 恰低于阈值：392*255/10000 ≈ 10.0 → 用 380 像素 ≈ 9.69 < 10 不触发
    just_below = energy_frame(base, 380)
    assert det.update(just_below, 1) is None
    assert det.active is False
    # 高于阈值：470 像素 ≈ 11.99 > 10 触发
    det2 = SwingDetector(ROI, FPS, trigger_thresh=10.0, release_thresh=1.0)
    det2.update(base, 0)
    above = energy_frame(base, 470)
    assert det2.update(above, 1) == "started"


def test_swing_quiet_counter_resets_on_energy():
    det = SwingDetector(ROI, FPS, trigger_thresh=10.0, release_thresh=5.0, post_roll_seconds=0.3)
    base = empty_frame()
    det.update(base, 0)
    burst = energy_frame(base, 784)  # ≈20
    assert det.update(burst, 1) == "started"
    quiet = empty_frame()
    # 安静 2 帧后又来一波能量 → quiet 计数清零，不结束
    det.update(quiet, 2)
    det.update(quiet, 3)
    burst2 = energy_frame(quiet, 470)
    assert det.update(burst2, 4) is None  # ≈12 > 5，quiet 清零
    # 之后保持 burst2 画面不变（能量 0 < 5），3 帧安静后结束
    for i in range(2):
        assert det.update(burst2.copy(), 5 + i) is None
    assert det.update(burst2.copy(), 7) == "ended"  # post_roll = 3 帧


def test_swing_pre_post_roll_frames_conversion():
    det = SwingDetector(ROI, 120.0, pre_roll_seconds=1.0, post_roll_seconds=1.0)
    assert det.pre_roll_frames == 120
    assert det.post_roll_frames == 120
