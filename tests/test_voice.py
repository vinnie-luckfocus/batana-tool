"""语音层测试：NullVoice 记录、SayVoice 队列/静音（真实 `say` 冒烟）。"""

from __future__ import annotations

import shutil
import time

import pytest

from app.voice import NullVoice, SayVoice, prompt_saved


def test_null_voice_records_and_mute():
    v = NullVoice()
    v.speak("你好")
    v.speak("紧急", priority=1)
    assert v.texts() == ["你好", "紧急"]
    assert v.history[1] == ("紧急", 1)
    v.set_muted(True)
    v.speak("不应记录")
    assert v.texts() == ["你好", "紧急"]


def test_prompt_saved_format():
    assert prompt_saved(3) == "已保存，第 3 段，请准备下一段"


@pytest.mark.skipif(shutil.which("say") is None, reason="非 macOS 环境")
def test_say_voice_speaks_serially_without_error():
    v = SayVoice()
    try:
        v.speak("一")
        v.speak("二", priority=1)  # 高优先级先播，但都已入队
        deadline = time.time() + 20
        while not v._queue.empty() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)  # 等最后一条播报进程
        assert v.errors == []
    finally:
        v.close()


@pytest.mark.skipif(shutil.which("say") is None, reason="非 macOS 环境")
def test_say_voice_mute_drops_pending():
    v = SayVoice()
    try:
        v.set_muted(True)
        v.speak("不应播放")
        assert v._queue.empty()
    finally:
        v.close()
