"""ClipWriter 产出完整性测试：MKV 可读回、帧数一致、时间戳行数一致、编码回退。"""

from __future__ import annotations

import csv
import json
import warnings

import cv2
import numpy as np
import pytest

import app.capture.clip_writer as cw
from app.capture import ClipWriter, count_video_frames

FPS = 30.0
SIZE = (96, 64)  # (宽, 高)


def make_items(count: int, seed: int = 0) -> list[tuple[int, int, np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(seed)
    items = []
    for i in range(count):
        left = rng.integers(0, 256, (SIZE[1], SIZE[0]), dtype=np.uint8)
        right = rng.integers(0, 256, (SIZE[1], SIZE[0]), dtype=np.uint8)
        items.append((i, i * 33_000_000, left, right))
    return items


def test_write_clip_outputs_complete(tmp_path):
    items = make_items(25)
    writer = ClipWriter(fps=FPS)
    paths = writer.write_clip(items, tmp_path / "clip1", meta={"camera": "synth"})
    assert paths.frame_count == 25
    for p in (paths.left_video, paths.right_video, paths.timestamps_csv, paths.capture_meta):
        assert p.is_file(), p
    # 视频可读回且帧数一致
    assert count_video_frames(paths.left_video) == 25
    assert count_video_frames(paths.right_video) == 25
    # timestamps.csv 行数一致（含表头 +1）
    with paths.timestamps_csv.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["frame_idx", "ts_ns"]
    assert len(rows) == 26
    assert rows[1] == ["0", "0"]
    assert rows[25] == ["24", str(24 * 33_000_000)]
    # capture_meta.json 字段
    meta = json.loads(paths.capture_meta.read_text(encoding="utf-8"))
    assert meta["fps"] == FPS
    assert meta["frame_count"] == 25
    assert meta["per_eye_resolution"] == [SIZE[0], SIZE[1]]
    assert meta["camera"] == "synth"


def test_ffv1_lossless_roundtrip(tmp_path):
    writer = ClipWriter(fps=FPS)
    paths = writer.write_clip(make_items(10), tmp_path / "clip")
    if paths.codec != "FFV1":
        pytest.skip("本机 OpenCV 不支持 FFV1，跳过无损校验")
    assert paths.left_video.suffix == ".mkv"
    assert json.loads(paths.capture_meta.read_text())["lossless"] is True
    cap = cv2.VideoCapture(str(paths.left_video))
    ok, first = cap.read()
    cap.release()
    assert ok
    gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY) if first.ndim == 3 else first
    expected = make_items(10)[0][2]
    assert bool((gray == expected).all()), "FFV1 应逐位无损"


def test_fallback_to_mp4v_warns(tmp_path, monkeypatch):
    # 模拟 FFV1 不可用：候选表换成 [坏编码, mp4v]
    monkeypatch.setattr(cw, "_CODEC_CANDIDATES", [("XXXX", ".mkv"), ("mp4v", ".mp4")])
    writer = ClipWriter(fps=FPS)
    with pytest.warns(RuntimeWarning, match="回退"):
        paths = writer.write_clip(make_items(5), tmp_path / "clip")
    assert paths.codec == "mp4v"
    assert paths.left_video.suffix == ".mp4"
    assert count_video_frames(paths.left_video) == 5


def test_empty_clip_raises(tmp_path):
    writer = ClipWriter(fps=FPS)
    with pytest.raises(ValueError, match="空片段"):
        writer.write_clip([], tmp_path / "clip")
