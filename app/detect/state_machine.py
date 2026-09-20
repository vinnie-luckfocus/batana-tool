"""采集编排状态机（PRD F5）：IDLE→READY→ARMED→SWING→SAVING→READY 循环。

纯 Python、无 Qt 依赖；事件驱动（feed_frame 逐帧喂入）；
为 UI 集成预留 listener 回调与可选 Voice 播报。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import numpy as np

from app.capture.ring_buffer import RingBuffer
from app.detect.presence import PresenceDetector
from app.detect.swing import SwingDetector
from app.voice.voice import (
    PROMPT_DISCARDED,
    PROMPT_ERROR,
    PROMPT_LEFT,
    PROMPT_READY,
    PROMPT_SWING,
    PROMPT_SWING_DONE,
    Voice,
    prompt_saved,
)

log = logging.getLogger(__name__)


class State(Enum):
    IDLE = "IDLE"      # 无人
    READY = "READY"    # 就位，语音 + 倒计时
    ARMED = "ARMED"    # 待挥棒，预录中
    SWING = "SWING"    # 挥棒中
    SAVING = "SAVING"  # 落盘
    ERROR = "ERROR"    # 相机/存储异常，可恢复


@dataclass
class Clip:
    """一段挥棒素材：帧区间 + 环形缓冲引用 + 序号。"""

    seq: int
    start_idx: int
    end_idx: int
    trigger_idx: int
    buffer: RingBuffer = field(repr=False)

    @property
    def frame_count(self) -> int:
        return self.end_idx - self.start_idx + 1

    def frames(self):
        """从缓冲提取本段帧序列；缓冲容量不足时旧帧可能已被覆盖。"""
        return self.buffer.extract(self.start_idx, self.end_idx)


@dataclass
class Transition:
    prev: State
    next: State
    reason: str
    clip: Clip | None = None


TransitionListener = Callable[[Transition], None]
ClipSaver = Callable[[Clip], None]


class CaptureStateMachine:
    """挥棒采集状态机。

    - feed_frame()：每帧调用，内部驱动就位/挥棒检测与状态迁移。
    - manual_start()/manual_stop()：检测失败兜底的手动开始/结束（PRD F5）。
    - discard()：误检立即丢弃重拍。
    - pause()/error()/recover()：人离开/异常路径。
    """

    def __init__(
        self,
        presence: PresenceDetector,
        swing: SwingDetector,
        buffer: RingBuffer,
        fps: float,
        voice: Voice | None = None,
        clip_saver: ClipSaver | None = None,
        countdown_seconds: float = 3.0,
    ) -> None:
        self.presence = presence
        self.swing = swing
        self.buffer = buffer
        self.fps = float(fps)
        self.voice = voice
        self.clip_saver = clip_saver
        self.countdown_frames = max(1, int(countdown_seconds * fps + 0.5))
        self.state = State.IDLE
        self.seq = 0
        self.clips: list[Clip] = []
        self.last_error: str | None = None
        self._listeners: list[TransitionListener] = []
        self._ready_elapsed = 0
        self._countdown_next = 0
        self._trigger_idx = -1

    # ---- 外部接口 ----

    def add_listener(self, fn: TransitionListener) -> None:
        """注册迁移回调（UI 层接信号用）：fn(Transition)。"""
        self._listeners.append(fn)

    def feed_frame(self, frame_idx: int, ts_ns: int, left: np.ndarray, right: np.ndarray) -> None:
        self.buffer.push(frame_idx, ts_ns, left, right)
        if self.state is State.ERROR:
            return
        present = self.presence.update(left)

        # 任意工作态人离开 → IDLE（SWING 中离开丢弃进行中片段）
        if not present and self.state in (State.READY, State.ARMED, State.SWING, State.SAVING):
            self.swing.reset()
            self._speak(PROMPT_LEFT, priority=1)
            self._transition(State.IDLE, reason="presence_lost")
            return

        if self.state is State.IDLE:
            if present:
                self._ready_elapsed = 0
                # +1 使首个整秒 tick（如 "3"）在倒计时第一帧即播报
                self._countdown_next = math.ceil(self.countdown_frames / self.fps) + 1
                self._speak(PROMPT_READY)
                self._transition(State.READY, reason="presence_settled")
            return

        if self.state is State.READY:
            self._ready_elapsed += 1
            remaining_s = (self.countdown_frames - self._ready_elapsed) / self.fps
            if remaining_s > 0:
                tick = math.ceil(remaining_s)
                if tick < self._countdown_next:
                    self._countdown_next = tick
                    self._speak(str(tick))
                return
            self._speak(PROMPT_SWING)
            self._transition(State.ARMED, reason="countdown_done")
            return

        if self.state is State.ARMED:
            if self.swing.update(left, frame_idx) == "started":
                self._trigger_idx = self.swing.trigger_idx
                self._transition(State.SWING, reason="swing_started")
            return

        if self.state is State.SWING:
            if self._trigger_idx >= 0 and self.swing.active:
                self.swing.update(left, frame_idx)
            if not self.swing.active and self._trigger_idx >= 0:
                self._finish_swing(frame_idx, reason="swing_ended")
            return

    def manual_start(self, frame_idx: int | None = None) -> None:
        """手动开始（兜底）：ARMED → SWING。"""
        if self.state is not State.ARMED:
            return
        latest = self.buffer.latest()
        idx = frame_idx if frame_idx is not None else (latest[0] if latest else 0)
        self._trigger_idx = idx
        self.swing._active = True  # 手动模式：由 manual_stop 结束
        self.swing._trigger_idx = idx
        self._transition(State.SWING, reason="manual_start")

    def manual_stop(self, frame_idx: int | None = None) -> None:
        """手动结束（兜底）：SWING → SAVING → READY。"""
        if self.state is not State.SWING:
            return
        latest = self.buffer.latest()
        idx = frame_idx if frame_idx is not None else (latest[0] if latest else self._trigger_idx)
        self.swing._active = False
        self._finish_swing(idx, reason="manual_stop")

    def discard(self) -> None:
        """误检丢弃重拍：SWING/ARMED → READY，不产出片段。"""
        if self.state not in (State.SWING, State.ARMED):
            return
        self.swing.reset()
        self._trigger_idx = -1
        self._speak(PROMPT_DISCARDED, priority=1)
        self._transition(State.READY, reason="discarded")

    def pause(self) -> None:
        """暂停：任意工作态 → IDLE。"""
        if self.state in (State.IDLE, State.ERROR):
            return
        self.swing.reset()
        self._transition(State.IDLE, reason="paused")

    def error(self, message: str) -> None:
        """异常：任意态 → ERROR。"""
        self.last_error = message
        self._speak(PROMPT_ERROR, priority=2)
        self._transition(State.ERROR, reason=f"error: {message}")

    def recover(self) -> None:
        """异常恢复：ERROR → IDLE。"""
        if self.state is State.ERROR:
            self.swing.reset()
            self.presence.reset()
            self._transition(State.IDLE, reason="recovered")

    # ---- 内部 ----

    def _finish_swing(self, end_idx: int, reason: str) -> None:
        self.seq += 1
        start_idx = self._trigger_idx - self.swing.pre_roll_frames
        span = self.buffer.span()
        if span:
            start_idx = max(start_idx, span[0])
        start_idx = max(start_idx, 0)
        clip = Clip(
            seq=self.seq,
            start_idx=start_idx,
            end_idx=end_idx,
            trigger_idx=self._trigger_idx,
            buffer=self.buffer,
        )
        self.clips.append(clip)
        self._trigger_idx = -1
        self.swing.reset()
        self._speak(PROMPT_SWING_DONE)
        self._transition(State.SAVING, reason=reason, clip=clip)
        try:
            if self.clip_saver is not None:
                self.clip_saver(clip)
        except Exception as e:
            self.clips.remove(clip)
            self.error(f"片段落盘失败: {e}")
            return
        self._speak(prompt_saved(clip.seq))
        self._transition(State.READY, reason="saved", clip=clip)
        self._ready_elapsed = 0
        self._countdown_next = math.ceil(self.countdown_frames / self.fps) + 1

    def _transition(self, next_state: State, reason: str, clip: Clip | None = None) -> None:
        prev = self.state
        self.state = next_state
        event = Transition(prev=prev, next=next_state, reason=reason, clip=clip)
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:  # listener 异常不打断状态机
                log.exception("状态机 listener 异常")

    def _speak(self, text: str, priority: int = 0) -> None:
        if self.voice is not None:
            self.voice.speak(text, priority=priority)
