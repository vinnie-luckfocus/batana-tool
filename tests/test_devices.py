"""设备枚举单元测试：ffmpeg -list_devices 输出解析与默认设备选择。"""

from __future__ import annotations

from app.capture.devices import VideoDevice, parse_device_list, pick_default

FFMPEG_SAMPLE = """[AVFoundation indev @ 0x7f92c14140] AVFoundation video devices:
[AVFoundation indev @ 0x7f92c14140] [0] FaceTime高清相机
[AVFoundation indev @ 0x7f92c14140] [1] USB Global Camera
[AVFoundation indev @ 0x7f92c14140] [2] “iiiv1nn1e_15”的相机
[AVFoundation indev @ 0x7f92c14140] [3] “iiiv1nn1e_15”的桌上视角相机
[AVFoundation indev @ 0x7f92c14140] [4] Capture screen 0
[AVFoundation indev @ 0x7f92c14140] AVFoundation audio devices:
[AVFoundation indev @ 0x7f92c14140] [0] MacBook Pro麦克风
[AVFoundation indev @ 0x7f92c14140] [1] “iiiv1nn1e_15”的麦克风
"""


def test_parse_video_section_only():
    devices = parse_device_list(FFMPEG_SAMPLE)
    names = [d.name for d in devices]
    assert names == [
        "FaceTime高清相机",
        "USB Global Camera",
        "“iiiv1nn1e_15”的相机",
        "“iiiv1nn1e_15”的桌上视角相机",
    ]  # 屏幕采集与音频设备被过滤
    assert devices[1].index == 1
    assert devices[1].is_stereo_module
    assert not devices[0].is_stereo_module


def test_parse_empty():
    assert parse_device_list("") == []
    assert parse_device_list("no devices here") == []


def test_pick_default_prefers_stereo_module():
    devices = parse_device_list(FFMPEG_SAMPLE)
    assert pick_default(devices).name == "USB Global Camera"


def test_pick_default_skips_facetime_when_no_module():
    devices = [VideoDevice(0, "FaceTime高清相机"), VideoDevice(2, "Some USB Cam")]
    assert pick_default(devices).index == 2


def test_pick_default_fallback_first():
    devices = [VideoDevice(0, "FaceTime高清相机")]
    assert pick_default(devices).index == 0
    assert pick_default([]) is None
