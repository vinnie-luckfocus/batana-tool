"""pytest 共享夹具：可控假检测器、合成帧工具。"""

from __future__ import annotations

import numpy as np
import pytest


class FakePresence:
    """可控就位检测器：present 由测试直接设置。"""

    def __init__(self, present: bool = False) -> None:
        self._present = present
        self.last_ratio = 1.0 if present else 0.0

    @property
    def present(self) -> bool:
        return self._present

    def set(self, present: bool) -> None:
        self._present = present
        self.last_ratio = 1.0 if present else 0.0

    def update(self, frame: np.ndarray) -> bool:
        return self._present

    def reset(self) -> None:
        pass


class FakeSwing:
    """可控挥棒检测器：在指定帧号触发/结束。"""

    def __init__(self, pre_roll_frames: int = 2, start_at: int | None = None, end_at: int | None = None) -> None:
        self.pre_roll_frames = pre_roll_frames
        self.start_at = start_at
        self.end_at = end_at
        self.active = False
        self.trigger_idx = -1
        self.last_energy = 0.0

    def update(self, frame: np.ndarray, frame_idx: int) -> str | None:
        if not self.active and self.start_at is not None and frame_idx >= self.start_at:
            self.active = True
            self.trigger_idx = frame_idx
            return "started"
        if self.active and self.end_at is not None and frame_idx >= self.end_at:
            self.active = False
            return "ended"
        return None

    def reset(self) -> None:
        self.active = False
        self.trigger_idx = -1


def make_frame(value: int = 0, shape: tuple[int, int] = (100, 200)) -> np.ndarray:
    """生成常量灰度帧（左/右目共用）。"""
    return np.full(shape, value, np.uint8)


@pytest.fixture
def fake_presence() -> FakePresence:
    return FakePresence()


@pytest.fixture
def frame() -> np.ndarray:
    return make_frame()
