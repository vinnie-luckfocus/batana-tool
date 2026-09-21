"""FfmpegUvcSource 单元测试：命令构造 / YUYV→MONO8 转换 / 预热与断流错误处理。"""

from __future__ import annotations

import io

import numpy as np
import pytest

import app.capture.ffmpeg_source as mod
from app.capture.ffmpeg_source import FfmpegUvcSource

W, H = 8, 4  # 小帧：yuyv422 每帧 8*4*2=64 字节


def make_frame(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=W * H * 2, dtype=np.uint8).tobytes()


class FakeProc:
    """模拟 ffmpeg 子进程：stdout 喂入预制字节流，stderr 为空。"""

    def __init__(self, cmd, stdout, stderr, bufsize=0):
        self.cmd = cmd
        self.stdout = io.BytesIO(FakeProc.payload)
        self.stderr = io.BytesIO(FakeProc.stderr_text)
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


FakeProc.payload = b""
FakeProc.stderr_text = b""


@pytest.fixture()
def patch_popen(monkeypatch):
    monkeypatch.setattr(mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)


def set_payload(*frames: bytes, stderr: bytes = b"") -> None:
    FakeProc.payload = b"".join(frames)
    FakeProc.stderr_text = stderr


def test_yuyv_to_gray_takes_luma():
    # YUYV：偶数字节 Y、奇数字节 U/V → 取 Y 后宽减半为原宽
    buf = bytes([10, 200, 20, 201, 30, 202, 40, 203])  # 4 像素
    gray = FfmpegUvcSource.yuyv_to_gray(buf, width=4, height=1)
    assert gray.shape == (1, 4)
    assert gray.tolist() == [[10, 20, 30, 40]]


def test_command_uses_device_name_and_mode(patch_popen):
    set_payload(make_frame(1) * 2)
    src = FfmpegUvcSource(device="USB Global Camera", width=W, height=H, fps=120.0)
    cmd = src.cmd if hasattr(src, "cmd") else src._cmd
    assert "-f" in cmd and "avfoundation" in cmd
    assert "USB Global Camera:none" in cmd
    assert f"{W}x{H}" in cmd
    assert "120" in cmd
    assert "yuyv422" in cmd
    src.close()


def test_primed_first_frame_then_stream(patch_popen):
    frames_in = [make_frame(s) for s in range(3)]
    set_payload(*frames_in)
    src = FfmpegUvcSource(device="cam", width=W, height=H, fps=120.0)
    got = []
    for idx, ts, gray in src.frames():
        got.append(gray)
        if len(got) == 3:
            break
    src.close()
    for expected, actual in zip(frames_in, got):
        assert np.array_equal(
            FfmpegUvcSource.yuyv_to_gray(expected, W, H), actual
        )


def test_open_failure_raises_with_stderr(patch_popen):
    set_payload(stderr=b"Selected video size (1280x400) is not supported")
    with pytest.raises(RuntimeError, match="不被支持"):
        FfmpegUvcSource(device="cam", width=W, height=H, fps=120.0)


def test_stream_break_raises(patch_popen):
    # 预热帧完整，第二帧只有半截 → 断流报错
    set_payload(make_frame(1), make_frame(2)[:10])
    src = FfmpegUvcSource(device="cam", width=W, height=H, fps=120.0)
    it = src.frames()
    next(it)
    with pytest.raises(RuntimeError, match="中断"):
        next(it)
    src.close()


def test_available_gate(monkeypatch):
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    assert not FfmpegUvcSource.available()
