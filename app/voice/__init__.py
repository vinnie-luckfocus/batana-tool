"""app.voice：TTS 抽象（Voice protocol）+ macOS say 实现 + 测试用 NullVoice。"""

from app.voice.say_voice import SayVoice
from app.voice.voice import (
    PROMPT_COUNTDOWN,
    PROMPT_DISCARDED,
    PROMPT_ERROR,
    PROMPT_ERROR_CAMERA,
    PROMPT_ERROR_STORAGE,
    PROMPT_LEFT,
    PROMPT_NO_SWING,
    PROMPT_READY,
    PROMPT_SWING,
    PROMPT_SWING_DONE,
    NullVoice,
    Voice,
    error_prompt,
    prompt_saved,
)

__all__ = [
    "PROMPT_COUNTDOWN",
    "PROMPT_DISCARDED",
    "PROMPT_ERROR",
    "PROMPT_ERROR_CAMERA",
    "PROMPT_ERROR_STORAGE",
    "PROMPT_LEFT",
    "PROMPT_NO_SWING",
    "PROMPT_READY",
    "PROMPT_SWING",
    "PROMPT_SWING_DONE",
    "NullVoice",
    "SayVoice",
    "Voice",
    "error_prompt",
    "prompt_saved",
]
