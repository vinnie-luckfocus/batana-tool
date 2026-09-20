"""语音提示文案（PRD F4）与 Voice 抽象。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---- 中文提示文案（PRD F4） ----
PROMPT_READY = "已就位，请准备"
PROMPT_COUNTDOWN = ["3", "2", "1"]
PROMPT_SWING = "请挥棒"
PROMPT_SWING_DONE = "挥棒完成"
PROMPT_LEFT = "人员离开，已暂停采集"
PROMPT_ERROR = "出现异常，请检查设备"
PROMPT_ERROR_CAMERA = "相机断开，请检查设备连接"
PROMPT_ERROR_STORAGE = "存储失败，请检查磁盘空间"
PROMPT_NO_SWING = "未检测到挥棒，请用力挥棒或使用手动开始"
PROMPT_DISCARDED = "已丢弃，请重新挥棒"

# 异常文案分类关键词（消息小写后匹配；存储优先于相机）
_STORAGE_KEYWORDS = ("存储", "落盘", "写入", "磁盘", "空间", "disk", "write", "storage")
_CAMERA_KEYWORDS = ("相机", "camera", "uvc", "设备", "打开", "断开", "读帧")


def error_prompt(message: str) -> str:
    """按异常消息分类语音提示：相机断开 / 存储失败 / 通用异常。"""
    msg = message.lower()
    if any(k in msg for k in _STORAGE_KEYWORDS):
        return PROMPT_ERROR_STORAGE
    if any(k in msg for k in _CAMERA_KEYWORDS):
        return PROMPT_ERROR_CAMERA
    return PROMPT_ERROR


def prompt_saved(seq: int) -> str:
    return f"已保存，第 {seq} 段，请准备下一段"


@runtime_checkable
class Voice(Protocol):
    """TTS 抽象：异步播报，永不阻塞采集流程。"""

    def speak(self, text: str, priority: int = 0) -> None:
        """播报文本；priority 越大越优先（默认 0）。"""
        ...

    def set_muted(self, muted: bool) -> None:
        ...

    def close(self) -> None:
        ...


class NullVoice:
    """测试用语音：不发声，只记录播报历史。"""

    def __init__(self) -> None:
        self.history: list[tuple[str, int]] = []
        self.muted = False

    def speak(self, text: str, priority: int = 0) -> None:
        if not self.muted:
            self.history.append((text, priority))

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def texts(self) -> list[str]:
        return [t for t, _ in self.history]

    def close(self) -> None:
        pass
