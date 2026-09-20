"""帧源抽象：UVC 相机与视频文件回放统一为 (frame_idx, ts_ns, sbs_frame) 迭代器。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

import cv2
import numpy as np

# 一帧 = (帧序号, 单调时钟纳秒时间戳, SBS 整帧灰度图)
FrameTuple = tuple[int, int, np.ndarray]


@runtime_checkable
class FrameSource(Protocol):
    """帧源协议：任何可迭代产出双目 SBS 帧的来源（相机、文件、合成器）。"""

    fps: float
    frame_size: tuple[int, int]  # (宽, 高)，SBS 整帧

    def frames(self) -> Iterator[FrameTuple]:
        """产出 (frame_idx, ts_ns, sbs_frame)，frame_idx 从 0 单调递增。"""
        ...

    def close(self) -> None:
        ...


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """统一到 MONO8：YUY2/BGR 取亮度通道，灰度原样返回。"""
    if frame.ndim == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


class UvcSource:
    """OpenCV UVC 相机帧源（macOS AVFoundation 后端）。

    目标模组：2560x800@120 SBS，MONO8 或 YUY2（取 Y）。MJPEG 仅冒烟用。
    """

    def __init__(
        self,
        device_index: int = 0,
        width: int = 2560,
        height: int = 800,
        fps: float = 120.0,
        pixel_format: str = "auto",  # auto / mono8 / yuy2 / mjpeg
    ) -> None:
        self.device_index = device_index
        self.fps = float(fps)
        self.frame_size = (width, height)
        self.pixel_format = pixel_format
        self._cap = cv2.VideoCapture(device_index, cv2.CAP_AVFOUNDATION)
        if not self._cap.isOpened():
            raise RuntimeError(f"无法打开 UVC 设备 index={device_index}")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_FPS, fps)
        if pixel_format == "mjpeg":
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        elif pixel_format == "yuy2":
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUY2"))
        elif pixel_format == "mono8":
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"GREY"))

    def frames(self) -> Iterator[FrameTuple]:
        idx = 0
        while True:
            ok, frame = self._cap.read()
            if not ok:
                raise RuntimeError(f"UVC 采集失败（设备 index={self.device_index} 可能已断开）")
            yield idx, time.monotonic_ns(), _to_gray(frame)
            idx += 1

    def close(self) -> None:
        self._cap.release()


class FileSource:
    """视频文件回放帧源：合成素材与端到端测试用，时间戳按 fps 合成。"""

    def __init__(self, path: str | Path, fps: float | None = None, loop: bool = False) -> None:
        self.path = Path(path)
        self._loop = loop
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise RuntimeError(f"无法打开视频文件: {self.path}")
        file_fps = self._cap.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps) if fps else (float(file_fps) if file_fps > 0 else 120.0)
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_size = (w, h)

    def frames(self) -> Iterator[FrameTuple]:
        idx = 0
        frame_ns = int(1_000_000_000 / self.fps)
        while True:
            ok, frame = self._cap.read()
            if not ok:
                if self._loop and idx > 0:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                return
            yield idx, idx * frame_ns, _to_gray(frame)
            idx += 1

    def close(self) -> None:
        self._cap.release()
