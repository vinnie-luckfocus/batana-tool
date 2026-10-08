"""视觉提示文案与分类（替代原语音提示，PRD F4 视觉化）。

采集引导全部改为预览画面上的大号视觉提示（见 app/ui/preview.py 的
cue 渲染）：倒计时数字 → 「挥棒！」→ 「录制中」→ 「已保存」。
视觉提示零延迟（帧驱动），从根本上消除语音播报与采集时序的错位。
"""

from __future__ import annotations

# ---- 提示类型（preview.set_cue 的 kind 参数） ----
CUE_COUNTDOWN = "countdown"  # 倒计时大号数字
CUE_ARMED = "armed"          # 待挥棒：绿色描边 + 大字，持续到挥棒开始
CUE_SWING = "swing"          # 录制中：红色描边 + 顶部标记
CUE_SAVED = "saved"          # 已保存：绿色闪屏，短暂显示
CUE_SAVING = "saving"        # 保存中：白色中字（落盘+沉淀等待期间）
CUE_WARN = "warn"            # 告警：橙色（人员离开/超时未挥棒/异常）

# ---- 文案 ----
TEXT_SWING = "挥棒！"
TEXT_RECORDING = "● 录制中"
TEXT_SAVED = "已保存 ✓"
TEXT_LEFT = "人员离开"
TEXT_NO_SWING = "请挥棒"
TEXT_DISCARDED = "已丢弃"
TEXT_SAVING = "保存中…"

# ---- 异常文案（沿用原语音分类语义） ----
PROMPT_ERROR = "出现异常，请检查设备"
PROMPT_ERROR_CAMERA = "相机断开，请检查设备连接"
PROMPT_ERROR_STORAGE = "存储失败，请检查磁盘空间"

# 异常文案分类关键词（消息小写后匹配；存储优先于相机）
_STORAGE_KEYWORDS = ("存储", "落盘", "写入", "磁盘", "空间", "disk", "write", "storage")
_CAMERA_KEYWORDS = ("相机", "camera", "uvc", "设备", "打开", "断开", "读帧")


def error_prompt(message: str) -> str:
    """按异常消息分类提示：相机断开 / 存储失败 / 通用异常。"""
    msg = message.lower()
    if any(k in msg for k in _STORAGE_KEYWORDS):
        return PROMPT_ERROR_STORAGE
    if any(k in msg for k in _CAMERA_KEYWORDS):
        return PROMPT_ERROR_CAMERA
    return PROMPT_ERROR
