"""预录环形缓冲：按秒 × fps 定容，线程安全，支持按帧序号区间提取。"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np

# 缓冲元素 = (帧序号, 时间戳 ns, 左目帧, 右目帧)
BufferItem = tuple[int, int, np.ndarray, np.ndarray]


class RingBuffer:
    """固定容量环形缓冲，满后覆盖最旧帧（wrap）。

    容量 = buffer_seconds × fps（向上取整，至少 1）。供挥棒 pre-roll 回溯使用。
    """

    def __init__(self, buffer_seconds: float, fps: float) -> None:
        capacity = max(1, int(buffer_seconds * fps + 0.5))
        self._capacity = capacity
        self._items: deque[BufferItem] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def push(self, frame_idx: int, ts_ns: int, left: np.ndarray, right: np.ndarray) -> None:
        with self._lock:
            self._items.append((frame_idx, ts_ns, left, right))

    def latest(self) -> BufferItem | None:
        with self._lock:
            return self._items[-1] if self._items else None

    def extract(self, start_idx: int, end_idx: int) -> list[BufferItem]:
        """按帧序号闭区间 [start_idx, end_idx] 提取，已被覆盖的旧帧静默跳过。"""
        with self._lock:
            return [item for item in self._items if start_idx <= item[0] <= end_idx]

    def span(self) -> tuple[int, int] | None:
        """当前缓冲覆盖的帧序号区间 (最旧, 最新)，空缓冲返回 None。"""
        with self._lock:
            if not self._items:
                return None
            return self._items[0][0], self._items[-1][0]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
