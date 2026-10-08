"""build_uvc_source 容错测试：模式协商失败自动纠正、OpenCV 回退按名解析序号。"""

from __future__ import annotations

import pytest

import app.ui.controller as ctl
from app.capture.devices import VideoDevice
from app.ui.settings import AppSettings


class FakeFfmpegSource:
    """模拟 ffmpeg 帧源：仅接受 1280x400@120，其余模式抛 RuntimeError。"""

    opened_modes: list[tuple] = []

    def __init__(self, device, width, height, fps):
        FakeFfmpegSource.opened_modes.append((width, height, fps))
        if (width, height, fps) != (1280, 400, 120.0):
            raise RuntimeError("模式不被支持")
        self.frame_size = (width, height)

    @staticmethod
    def available() -> bool:
        return True

    def close(self) -> None:
        pass


class FakeUvcSource:
    opened: list[int] = []

    def __init__(self, device_index, width, height, fps, pixel_format):
        FakeUvcSource.opened.append(device_index)

    def close(self) -> None:
        pass


@pytest.fixture()
def patch_sources(monkeypatch):
    FakeFfmpegSource.opened_modes = []
    FakeUvcSource.opened = []
    monkeypatch.setattr(ctl, "FfmpegUvcSource", FakeFfmpegSource)
    monkeypatch.setattr(ctl, "UvcSource", FakeUvcSource)
    monkeypatch.setattr(
        ctl, "list_video_devices",
        lambda: [VideoDevice(0, "FaceTime高清相机"), VideoDevice(1, "USB Global Camera")],
    )


def test_bad_mode_auto_corrects_to_fallback(patch_sources, tmp_path):
    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.capture_width, s.capture_height = 2560, 800  # 旧版残留
    src = ctl.build_uvc_source(s)
    assert isinstance(src, FakeFfmpegSource)
    assert src.frame_size == (1280, 400)
    # 设置已回写纠正
    assert (s.capture_width, s.capture_height, s.capture_fps) == (1280, 400, 120.0)
    # 先试了配置模式，再试 fallback
    assert FakeFfmpegSource.opened_modes[0] == (2560, 800, 120.0)
    assert FakeFfmpegSource.opened_modes[-1] == (1280, 400, 120.0)


def test_good_mode_no_retry(patch_sources, tmp_path):
    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.capture_width, s.capture_height = 1280, 400
    src = ctl.build_uvc_source(s)
    assert isinstance(src, FakeFfmpegSource)
    assert len(FakeFfmpegSource.opened_modes) == 1


def test_opencv_fallback_resolves_index_by_name(patch_sources, monkeypatch, tmp_path):
    monkeypatch.setattr(FakeFfmpegSource, "available", staticmethod(lambda: False))
    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.camera_index = 2  # 漂移的旧序号
    s.camera_name = "USB Global Camera"
    src = ctl.build_uvc_source(s)
    assert isinstance(src, FakeUvcSource)
    assert FakeUvcSource.opened == [1]  # 按名解析到当前序号


def test_forced_ffmpeg_backend_raises(patch_sources, tmp_path):
    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.capture_backend = "ffmpeg"
    s.capture_width, s.capture_height = 2560, 800
    with pytest.raises(RuntimeError):
        ctl.build_uvc_source(s)
