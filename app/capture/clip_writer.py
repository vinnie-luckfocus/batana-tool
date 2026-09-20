"""片段落盘写出器：左右目无损视频 + 逐帧时间戳 + 采集参数。

编码策略：FFV1/MKV 优先（无损）；不可用时回退 mp4v/mp4 并发出 warning。
"""

from __future__ import annotations

import csv
import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from app.capture.ring_buffer import BufferItem

# (fourcc 标签, 容器扩展名)，按优先级排列
_CODEC_CANDIDATES = [("FFV1", ".mkv"), ("mp4v", ".mp4")]


@dataclass
class ClipPaths:
    """一段素材落盘后的文件清单。"""

    out_dir: Path
    left_video: Path
    right_video: Path
    timestamps_csv: Path
    capture_meta: Path
    codec: str
    frame_count: int


def _try_open_writer(path: Path, codec: str, fps: float, size: tuple[int, int]) -> cv2.VideoWriter | None:
    fourcc = cv2.VideoWriter_fourcc(*codec)
    writer = cv2.VideoWriter(str(path), fourcc, fps, size, isColor=False)
    if not writer.isOpened():
        writer.release()
        return None
    return writer


class ClipWriter:
    """把 (idx, ts_ns, left, right) 帧序列写为一段素材目录。"""

    def __init__(self, fps: float, codec: str | None = None) -> None:
        """
        codec: 指定编码（"FFV1" / "mp4v"）；None 表示按优先级自动探测。
        """
        self.fps = float(fps)
        self._forced_codec = codec

    def _resolve_codec(self, out_dir: Path, size: tuple[int, int]) -> tuple[str, str]:
        candidates = _CODEC_CANDIDATES
        if self._forced_codec:
            ext = next((e for c, e in _CODEC_CANDIDATES if c == self._forced_codec), ".mkv")
            candidates = [(self._forced_codec, ext)]
        for codec, ext in candidates:
            probe = out_dir / f".probe{ext}"
            writer = _try_open_writer(probe, codec, self.fps, size)
            if writer is not None:
                writer.release()
                probe.unlink(missing_ok=True)
                return codec, ext
            probe.unlink(missing_ok=True)
        raise RuntimeError(f"无可用视频编码器（已尝试 {[c for c, _ in candidates]}）")

    def write_clip(
        self,
        frames: Iterable[BufferItem],
        out_dir: str | Path,
        meta: dict[str, Any] | None = None,
    ) -> ClipPaths:
        """写出整段素材；frames 至少 1 帧，否则 ValueError。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        items = list(frames)
        if not items:
            raise ValueError("空片段：至少需要 1 帧")
        h, w = items[0][2].shape[:2]

        codec, ext = self._resolve_codec(out, (w, h))
        if codec != _CODEC_CANDIDATES[0][0]:
            warnings.warn(
                f"FFV1 不可用，回退到 {codec}（有损编码，仅应急使用）",
                RuntimeWarning,
                stacklevel=2,
            )

        left_path = out / f"left{ext}"
        right_path = out / f"right{ext}"
        left_writer = _try_open_writer(left_path, codec, self.fps, (w, h))
        right_writer = _try_open_writer(right_path, codec, self.fps, (w, h))
        if left_writer is None or right_writer is None:
            raise RuntimeError(f"编码器 {codec} 探测成功但正式打开失败")
        try:
            for _, _, left, right in items:
                left_writer.write(left)
                right_writer.write(right)
        finally:
            left_writer.release()
            right_writer.release()

        ts_path = out / "timestamps.csv"
        with ts_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["frame_idx", "ts_ns"])
            for idx, ts_ns, _, _ in items:
                writer.writerow([idx, ts_ns])

        meta_path = out / "capture_meta.json"
        capture_meta: dict[str, Any] = {
            "fps": self.fps,
            "codec": codec,
            "lossless": codec == "FFV1",
            "frame_count": len(items),
            "per_eye_resolution": [w, h],
            "pixel_format": "MONO8",
            **(meta or {}),
        }
        meta_path.write_text(
            json.dumps(capture_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ClipPaths(
            out_dir=out,
            left_video=left_path,
            right_video=right_path,
            timestamps_csv=ts_path,
            capture_meta=meta_path,
            codec=codec,
            frame_count=len(items),
        )


def count_video_frames(path: str | Path) -> int:
    """读回视频统计帧数（完整性校验用）。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    n = 0
    while True:
        ok, _ = cap.read()
        if not ok:
            break
        n += 1
    cap.release()
    return n
