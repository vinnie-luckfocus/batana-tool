"""采集状态机测试：全迁移路径（含人离开中断、手动丢弃重拍、异常恢复）。"""

from __future__ import annotations

import pytest

from app.capture import RingBuffer
from app.detect import CaptureStateMachine, Clip, State
from app.voice import (
    PROMPT_DISCARDED,
    PROMPT_LEFT,
    PROMPT_READY,
    PROMPT_SWING,
    PROMPT_SWING_DONE,
    NullVoice,
    prompt_saved,
)
from tests.conftest import FakePresence, FakeSwing, make_frame

FPS = 10.0


def build(
    presence: FakePresence | None = None,
    swing: FakeSwing | None = None,
    countdown_seconds: float = 3.0,
    clip_saver=None,
) -> tuple[CaptureStateMachine, FakePresence, FakeSwing, NullVoice, list]:
    p = presence or FakePresence()
    s = swing or FakeSwing()
    voice = NullVoice()
    events: list = []
    sm = CaptureStateMachine(
        p, s, RingBuffer(3.0, FPS), FPS,
        voice=voice, clip_saver=clip_saver, countdown_seconds=countdown_seconds,
    )
    sm.add_listener(events.append)
    return sm, p, s, voice, events


def feed(sm: CaptureStateMachine, start: int, count: int) -> int:
    for i in range(start, start + count):
        f = make_frame()
        sm.feed_frame(i, i * 100_000_000, f, f)
    return start + count


def test_full_cycle_idle_to_ready():
    sm, p, _, voice, events = build(countdown_seconds=3.0)
    assert sm.state is State.IDLE
    p.set(True)
    feed(sm, 0, 1)
    assert sm.state is State.READY
    assert PROMPT_READY in voice.texts()
    assert (events[-1].prev, events[-1].next) == (State.IDLE, State.READY)


def test_countdown_ticks_then_armed():
    sm, p, _, voice, _ = build(countdown_seconds=3.0)
    p.set(True)
    feed(sm, 0, 31)  # 30 帧倒计时 + 1
    assert sm.state is State.ARMED
    texts = voice.texts()
    for tick in ("3", "2", "1"):
        assert tick in texts
    assert PROMPT_SWING in texts
    # 倒计时顺序：3 在 2 前，2 在 1 前，1 在请挥棒前
    assert texts.index("3") < texts.index("2") < texts.index("1") < texts.index(PROMPT_SWING)


def test_happy_path_produces_clip():
    swing = FakeSwing(pre_roll_frames=5, start_at=35, end_at=45)
    saved: list[Clip] = []
    sm, p, _, voice, events = build(swing=swing, countdown_seconds=3.0, clip_saver=saved.append)
    p.set(True)
    feed(sm, 0, 50)
    assert sm.state is State.READY  # SAVING 后循环回 READY
    assert len(saved) == 1
    clip = saved[0]
    assert clip.seq == 1
    assert clip.trigger_idx == 35
    assert clip.start_idx == 30  # trigger - pre_roll
    assert clip.end_idx == 45
    assert len(clip.frames()) == 16
    texts = voice.texts()
    assert PROMPT_SWING_DONE in texts
    assert prompt_saved(1) in texts
    path = [(e.prev, e.next) for e in events]
    assert (State.ARMED, State.SWING) in path
    assert (State.SWING, State.SAVING) in path
    assert (State.SAVING, State.READY) in path


def test_person_leaves_during_ready_goes_idle():
    sm, p, _, voice, _ = build(countdown_seconds=3.0)
    p.set(True)
    idx = feed(sm, 0, 5)
    assert sm.state is State.READY
    p.set(False)
    feed(sm, idx, 1)
    assert sm.state is State.IDLE
    assert PROMPT_LEFT in voice.texts()


def test_person_leaves_during_swing_discards_clip():
    swing = FakeSwing(pre_roll_frames=5, start_at=35, end_at=45)
    saved: list[Clip] = []
    sm, p, _, voice, _ = build(swing=swing, countdown_seconds=3.0, clip_saver=saved.append)
    p.set(True)
    idx = feed(sm, 0, 40)
    assert sm.state is State.SWING
    p.set(False)
    feed(sm, idx, 1)
    assert sm.state is State.IDLE
    assert saved == []  # 中断不产出片段
    assert sm.clips == []
    assert PROMPT_LEFT in voice.texts()


def test_discard_and_retake():
    swing = FakeSwing(pre_roll_frames=5, start_at=35, end_at=None)
    saved: list[Clip] = []
    sm, p, s, voice, _ = build(swing=swing, countdown_seconds=3.0, clip_saver=saved.append)
    p.set(True)
    feed(sm, 0, 40)
    assert sm.state is State.SWING
    sm.discard()
    assert sm.state is State.READY
    assert saved == []
    assert PROMPT_DISCARDED in voice.texts()
    # 重拍：倒计时后再次 ARMED → 触发 → 结束 → 产出
    s.start_at, s.end_at = 75, 85
    feed(sm, 40, 50)
    assert len(saved) == 1
    assert saved[0].seq == 1
    assert sm.state is State.READY


def test_manual_start_stop_fallback():
    saved: list[Clip] = []
    sm, p, _, _, _ = build(countdown_seconds=1.0, clip_saver=saved.append)
    p.set(True)
    idx = feed(sm, 0, 11)  # 10 帧倒计时 → ARMED
    assert sm.state is State.ARMED
    sm.manual_start()
    assert sm.state is State.SWING
    idx = feed(sm, idx, 5)
    sm.manual_stop()
    assert sm.state is State.READY
    assert len(saved) == 1
    assert saved[0].end_idx >= saved[0].start_idx


def test_error_and_recover():
    sm, p, _, voice, _ = build()
    p.set(True)
    feed(sm, 0, 1)
    sm.error("相机断开")
    assert sm.state is State.ERROR
    assert sm.last_error == "相机断开"
    # ERROR 态忽略帧
    feed(sm, 1, 10)
    assert sm.state is State.ERROR
    sm.recover()
    assert sm.state is State.IDLE


def test_clip_saver_failure_goes_error():
    def bad_saver(clip: Clip) -> None:
        raise OSError("磁盘已满")

    swing = FakeSwing(pre_roll_frames=2, start_at=35, end_at=40)
    sm, p, _, voice, _ = build(swing=swing, countdown_seconds=1.0, clip_saver=bad_saver)
    p.set(True)
    feed(sm, 0, 45)
    assert sm.state is State.ERROR
    assert "磁盘已满" in (sm.last_error or "")
    assert sm.clips == []  # 失败片段被回滚


def test_pause_from_working_state():
    sm, p, _, _, _ = build(countdown_seconds=3.0)
    p.set(True)
    feed(sm, 0, 5)
    sm.pause()
    assert sm.state is State.IDLE


def test_pre_roll_clamped_to_buffer():
    # 缓冲只够 1s（10 帧），pre_roll 5 帧但早期帧已被覆盖
    swing = FakeSwing(pre_roll_frames=8, start_at=12, end_at=15)
    p = FakePresence()
    saved: list[Clip] = []
    sm, _, _, _, _ = build(presence=p, swing=swing, countdown_seconds=0.2, clip_saver=saved.append)
    sm.buffer = RingBuffer(1.0, FPS)  # 容量 10
    p.set(True)
    feed(sm, 0, 16)  # idx 15 结束挥棒；不再喂帧，避免 FakeSwing 阈值重复触发
    assert len(saved) == 1
    clip = saved[0]
    # trigger=12，pre_roll=8 → 期望 4，但容量 10 的缓冲只覆盖 [6,15]，钳到 6
    assert clip.start_idx == 6
    frames = clip.frames()
    assert len(frames) == 10  # 受缓冲容量限制


def test_listener_receives_all_transitions():
    swing = FakeSwing(pre_roll_frames=2, start_at=15, end_at=20)
    sm, p, _, _, events = build(swing=swing, countdown_seconds=1.0)
    p.set(True)
    feed(sm, 0, 25)
    reasons = [e.reason for e in events]
    assert reasons[:1] == ["presence_settled"]
    assert "countdown_done" in reasons
    assert "swing_started" in reasons
    assert "swing_ended" in reasons
    assert "saved" in reasons
    saved_events = [e for e in events if e.reason == "saved"]
    assert saved_events[0].clip is not None
