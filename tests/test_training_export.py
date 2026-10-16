"""训练包导出测试：PNG 数量/命名、annotations 像素坐标与 manual 标记、null 目。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.capture import ClipWriter
from app.pose import StubPoseEstimator, write_pose2d
from app.pose.model import KEYPOINT_NAMES, Keypoint, PoseFrame
from app.session.training_export import export_training_pack
from conftest import make_frame

FPS = 10.0
SIZE = (96, 64)  # (w, h)


def _make_clip(tmp_path, n: int = 6):
    clip_dir = tmp_path / "sess_test01"
    items = [
        (i, int(i / FPS * 1e9),
         make_frame(30 + i, (SIZE[1], SIZE[0])),
         make_frame(60 + i, (SIZE[1], SIZE[0])))
        for i in range(n)
    ]
    ClipWriter(fps=FPS).write_clip(items, clip_dir)
    return clip_dir


def _pose_frames(indices, manual_at: tuple[int, int] | None = None):
    frames = []
    for i in indices:
        kps = [Keypoint(name=name, x=0.25, y=0.5, visibility=0.9)
               for name in KEYPOINT_NAMES]
        f = PoseFrame(frame_index=i, timestamp_ms=i / FPS * 1000.0,
                      confidence=0.9, keypoints=kps)
        if manual_at and i == manual_at[0]:
            f.correct(manual_at[1], 0.75, 0.25)
        frames.append(f)
    return frames


def test_training_pack_structure(tmp_path):
    clip = _make_clip(tmp_path)
    write_pose2d(clip / "pose2d.json", "stub", FPS, _pose_frames([1, 2, 3], manual_at=(2, 15)))
    write_pose2d(clip / "pose2d_right.json", "stub", FPS, _pose_frames([2]))

    out = export_training_pack(clip, tmp_path / "training", trim=(1, 3))
    assert out == tmp_path / "training" / clip.name

    # PNG：trim 段 3 帧 × 双目，帧号 6 位
    pngs = sorted(p.name for p in (out / "images").iterdir())
    expected = [f"{eye}_{i:06d}.png" for i in (1, 2, 3) for eye in ("left", "right")]
    assert sorted(pngs) == sorted(expected)

    ann = json.loads((out / "annotations.json").read_text())
    assert ann["format_version"] == "1.0"
    assert ann["session_id"] == clip.name
    assert ann["fps"] == FPS
    assert ann["per_eye_resolution"] == [SIZE[0], SIZE[1]]
    assert ann["trim"] == {"start_frame": 1, "end_frame": 3}
    assert len(ann["frames"]) == 3

    f1, f2, f3 = ann["frames"]
    assert f1["frame_index"] == 1
    # 右目只有帧 2 有骨架：帧 1/3 写 null
    assert f1["eyes"]["right"] is None
    assert f3["eyes"]["right"] is None
    assert f2["eyes"]["right"] is not None
    # 左目三帧都有；坐标为像素值（0.25×96=24, 0.5×64=32）
    kp = f1["eyes"]["left"]["keypoints"][0]
    assert kp["name"] == "nose"
    assert kp["x"] == pytest.approx(24.0)
    assert kp["y"] == pytest.approx(32.0)
    assert "manual" not in kp
    # 帧 2 左目 kp15 经人工修正：保留修正值（像素）与 manual 标记
    kp15 = f2["eyes"]["left"]["keypoints"][15]
    assert kp15["manual"] is True
    assert kp15["x"] == pytest.approx(0.75 * SIZE[0])
    assert kp15["y"] == pytest.approx(0.25 * SIZE[1])
    assert kp15["auto"]["x"] == pytest.approx(0.25)  # auto 保留归一化原值

    meta = json.loads((out / "meta.json").read_text())
    assert meta["frame_count"] == 3
    assert meta["source_clip"] == str(clip)
    assert meta["exported_at"]


def test_training_pack_default_full_range(tmp_path):
    clip = _make_clip(tmp_path, n=4)
    # 完全无骨架：双目全 null，但 PNG 照常导出
    out = export_training_pack(clip, tmp_path / "training")
    ann = json.loads((out / "annotations.json").read_text())
    assert len(ann["frames"]) == 4
    assert ann["trim"] == {"start_frame": 0, "end_frame": 3}
    assert all(f["eyes"]["left"] is None and f["eyes"]["right"] is None
               for f in ann["frames"])
    assert len(list((out / "images").glob("*.png"))) == 8
