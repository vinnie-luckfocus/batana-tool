"""UVC 设备枚举：macOS 下解析 ffmpeg avfoundation 设备列表。

ffmpeg -f avfoundation -list_devices true -i "" 的输出（stderr）形如：

    [AVFoundation indev @ 0x...] AVFoundation video devices:
    [AVFoundation indev @ 0x...] [0] FaceTime高清相机
    [AVFoundation indev @ 0x...] [1] USB Global Camera
    [AVFoundation indev @ 0x...] AVFoundation audio devices:
    ...

解析逻辑与进程调用分离，可无头测试。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

# 已知的双目模组名称（HBVCAM-W2237-2 等 SunplusIT 整模组）
STEREO_MODULE_NAMES = ("USB Global Camera",)

_DEVICE_LINE = re.compile(r"\] \[(\d+)\] (.+?)\s*$")


@dataclass(frozen=True)
class VideoDevice:
    index: int
    name: str

    @property
    def is_screen(self) -> bool:
        return self.name.startswith("Capture screen")

    @property
    def is_stereo_module(self) -> bool:
        return any(k in self.name for k in STEREO_MODULE_NAMES)


def parse_device_list(output: str) -> list[VideoDevice]:
    """解析 ffmpeg -list_devices 输出，仅取 video 段，过滤屏幕采集项。"""
    devices: list[VideoDevice] = []
    in_video = False
    for line in output.splitlines():
        if "AVFoundation video devices:" in line:
            in_video = True
            continue
        if "AVFoundation audio devices:" in line:
            in_video = False
            continue
        if in_video:
            m = _DEVICE_LINE.search(line)
            if m:
                dev = VideoDevice(index=int(m.group(1)), name=m.group(2))
                if not dev.is_screen:
                    devices.append(dev)
    return devices


def list_video_devices(timeout: float = 10.0) -> list[VideoDevice]:
    """枚举 avfoundation 视频设备；ffmpeg 缺失或失败时返回空列表。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return parse_device_list(proc.stderr + "\n" + proc.stdout)


def pick_default(devices: list[VideoDevice]) -> VideoDevice | None:
    """默认选择：优先双目模组，其次第一个非 FaceTime 外设，否则第一台。"""
    if not devices:
        return None
    for d in devices:
        if d.is_stereo_module:
            return d
    for d in devices:
        if "FaceTime" not in d.name:
            return d
    return devices[0]
