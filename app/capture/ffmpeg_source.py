"""ffmpeg AVFoundation 帧源：macOS 上绕开 OpenCV 的 AVCaptureSession preset 协商。

背景（2026-09-21 实测，HBVCAM-W2237-2 / OV9281 双目模组）：
- OpenCV CAP_AVFOUNDATION 只能按 preset 协商，请求 1280x400@120 会静默
  回退到 1280x720@30；
- ffmpeg avfoundation 直通 AVCaptureDeviceFormat，能选中模组的任意模式，
  YUY2 实测 2560x720@60 满速、1280x400 档位 75fps（AVFoundation 上限，
  设备描述符标称 120fps，满速 120 需在 Linux/v4l2 上验证）；
- AVFoundation 不暴露 MJPEG，macOS 侧统一走 yuyv422（取 Y 通道即 MONO8）。

帧读取：ffmpeg 输出 rawvideo（yuyv422，每帧 width*height*2 字节）到管道，
偶数字节即亮度 Y，直接得到 MONO8 灰度帧。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from typing import Iterator

import numpy as np

from app.capture.frame_source import FrameTuple

DEFAULT_DEVICE_NAME = "USB Global Camera"


class FfmpegUvcSource:
    """ffmpeg avfoundation 双目帧源（macOS 专用，Linux 请用 OpenCV V4L2）。"""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE_NAME,
        width: int = 1280,
        height: int = 400,
        fps: float = 120.0,
        pixel_format: str = "yuyv422",
    ) -> None:
        if platform.system() != "Darwin":
            raise RuntimeError("FfmpegUvcSource 仅支持 macOS（avfoundation）")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg（brew install ffmpeg）")
        self.device = device
        self.frame_size = (width, height)
        self.fps = float(fps)
        self.pixel_format = pixel_format
        self._frame_bytes = width * height * 2 if pixel_format == "yuyv422" else 0
        if self._frame_bytes == 0:
            raise RuntimeError(f"暂不支持的像素格式: {pixel_format}（仅 yuyv422）")
        self._cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-pixel_format", pixel_format,
            "-framerate", str(int(fps)),
            "-video_size", f"{width}x{height}",
            "-i", f"{device}:none",
            "-f", "rawvideo", "pipe:1",
        ]
        self._proc = subprocess.Popen(
            self._cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=self._frame_bytes * 4,
        )
        # 预热：构造时同步读首帧——模式不被支持时 ffmpeg 立即退出（EOF），
        # 让 build_uvc_source 能在构造期回退 OpenCV，而不是采集中途才报错。
        first = self._read_exact(self._frame_bytes)
        if len(first) < self._frame_bytes:
            raise self._fail(
                f"ffmpeg 未能打开设备 {device!r} 或模式 "
                f"{width}x{height}@{fps:.0f} 不被支持"
            )
        self._primed = first

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and shutil.which("ffmpeg") is not None

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        assert self._proc.stdout is not None
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _fail(self, msg: str) -> RuntimeError:
        stderr = ""
        if self._proc.stderr is not None:
            try:
                stderr = self._proc.stderr.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
        self.close()
        detail = f"：{stderr}" if stderr else ""
        return RuntimeError(f"{msg}{detail}")

    @staticmethod
    def yuyv_to_gray(buf: bytes | np.ndarray, width: int, height: int) -> np.ndarray:
        """yuyv422 原始字节 → MONO8（取偶数字节 Y 通道）。"""
        arr = np.frombuffer(buf, dtype=np.uint8)
        return arr.reshape(height, width * 2)[:, 0::2]

    def frames(self) -> Iterator[FrameTuple]:
        idx = 0
        primed, self._primed = self._primed, None
        yield idx, time.monotonic_ns(), self.yuyv_to_gray(primed, *self.frame_size)
        idx += 1
        while True:
            buf = self._read_exact(self._frame_bytes)
            if len(buf) < self._frame_bytes:
                raise self._fail("采集中断（设备可能已断开）")
            yield idx, time.monotonic_ns(), self.yuyv_to_gray(buf, *self.frame_size)
            idx += 1

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
