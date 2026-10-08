"""RingBuffer 边界测试：满、wrap、区间提取。"""

from __future__ import annotations

import threading

import numpy as np

from app.capture import RingBuffer
from tests.conftest import make_frame


def push_n(buf: RingBuffer, start: int, count: int) -> None:
    for i in range(start, start + count):
        buf.push(i, i * 1_000_000, make_frame(i % 255), make_frame((i * 2) % 255))


def test_capacity_from_seconds_times_fps():
    assert RingBuffer(3.0, 120.0).capacity == 360
    assert RingBuffer(0.5, 10.0).capacity == 5
    assert RingBuffer(0.01, 10.0).capacity == 1  # 至少 1


def test_push_and_len_until_full():
    buf = RingBuffer(1.0, 10.0)
    push_n(buf, 0, 7)
    assert len(buf) == 7
    push_n(buf, 7, 3)
    assert len(buf) == 10  # 满


def test_wrap_overwrites_oldest():
    buf = RingBuffer(1.0, 10.0)
    push_n(buf, 0, 10)
    push_n(buf, 10, 5)  # 覆盖 0..4
    assert len(buf) == 10
    assert buf.span() == (5, 14)
    latest = buf.latest()
    assert latest is not None and latest[0] == 14


def test_extract_closed_interval():
    buf = RingBuffer(2.0, 10.0)
    push_n(buf, 0, 15)
    items = buf.extract(3, 7)
    assert [it[0] for it in items] == [3, 4, 5, 6, 7]
    # 帧内容随 idx 携带
    assert items[0][2][0, 0] == 3
    assert items[0][3][0, 0] == 6


def test_extract_skips_overwritten_frames():
    buf = RingBuffer(1.0, 10.0)  # 容量 10
    push_n(buf, 0, 15)  # 覆盖后剩 5..14
    items = buf.extract(0, 14)
    assert [it[0] for it in items] == list(range(5, 15))


def test_extract_empty_and_out_of_range():
    buf = RingBuffer(1.0, 10.0)
    assert buf.extract(0, 10) == []
    assert buf.span() is None
    assert buf.latest() is None
    push_n(buf, 0, 5)
    assert buf.extract(100, 200) == []


def test_thread_safety_concurrent_push_extract():
    buf = RingBuffer(2.0, 50.0)  # 容量 100
    errors: list[Exception] = []

    def producer(offset: int) -> None:
        try:
            push_n(buf, offset, 500)
        except Exception as e:
            errors.append(e)

    def consumer() -> None:
        try:
            for _ in range(500):
                buf.extract(0, 10_000)
                buf.span()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=producer, args=(i * 1000,)) for i in range(2)]
    threads += [threading.Thread(target=consumer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(buf) == 100  # 最终满容量，无越界
