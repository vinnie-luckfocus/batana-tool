"""export_session 测试：产出结构、内置契约校验、batana-core 全量校验。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from app.capture import ClipWriter, RingBuffer, count_video_frames
from app.detect import Clip
from app.pose import StubPoseEstimator
from app.session import (
    build_session_json,
    core_validator_available,
    export_session,
    new_session_id,
    validate_session_builtin,
    validate_with_core,
)

FPS = 30.0
SIZE = (96, 64)


def make_clip(frame_count: int = 30) -> Clip:
    buf = RingBuffer(2.0, FPS)
    rng = np.random.default_rng(7)
    for i in range(frame_count):
        left = rng.integers(0, 256, (SIZE[1], SIZE[0]), dtype=np.uint8)
        right = rng.integers(0, 256, (SIZE[1], SIZE[0]), dtype=np.uint8)
        buf.push(i, i * 33_000_000, left, right)
    return Clip(seq=1, start_idx=0, end_idx=frame_count - 1, trigger_idx=10, buffer=buf)


def make_pose(clip: Clip):
    est = StubPoseEstimator()
    return [est.estimate(None, i, i / FPS * 1000) for i in range(clip.frame_count)]  # type: ignore[arg-type]


def test_export_produces_contract_directory(tmp_path):
    clip = make_clip()
    pose = make_pose(clip)
    out_dir = export_session(
        clip, tmp_path, pose_frames=pose, pose_model=StubPoseEstimator.model_name,
        writer=ClipWriter(fps=FPS),
    )
    assert out_dir.parent == tmp_path / "sessions"
    assert re.match(r"^sess_[0-9A-HJKMNP-TV-Z]{26}$", out_dir.name)
    expected = {"session.json", "timestamps.csv", "capture_meta.json", "pose2d.json"}
    names = {p.name for p in out_dir.iterdir()}
    assert expected <= names
    assert {"left.mkv", "right.mkv"} <= names or {"left.mp4", "right.mp4"} <= names

    session = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    # 内置校验通过
    assert validate_session_builtin(session) == []
    # 关键字段对齐契约
    assert session["schema_version"] == "1.0"
    assert session["meta"]["session_id"] == out_dir.name
    assert session["meta"]["pipeline"] == "pro-stereo"
    assert session["meta"]["duration_ms"] == 1000  # 30 帧 / 30fps
    assert session["video"]["fps"] == FPS
    assert session["video"]["resolution"] == [SIZE[0], SIZE[1]]
    assert re.match(r"^[0-9a-f]{64}$", session["video"]["sha256"])
    assert session["pose2d"]["frame_rate"] == FPS
    assert len(session["pose2d"]["frames"]) == 30
    assert len(session["pose2d"]["frames"][0]["keypoints"]) == 33

    # 视频与时间戳行数一致
    left_video = next(out_dir.glob("left.*"))
    assert count_video_frames(left_video) == 30
    ts_lines = (out_dir / "timestamps.csv").read_text().strip().splitlines()
    assert len(ts_lines) == 31


def test_export_without_pose(tmp_path):
    out_dir = export_session(make_clip(), tmp_path, writer=ClipWriter(fps=FPS))
    session = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    assert "pose2d" not in session
    assert not (out_dir / "pose2d.json").exists()
    assert validate_session_builtin(session) == []


def test_export_empty_clip_raises(tmp_path):
    clip = Clip(seq=1, start_idx=5, end_idx=9, trigger_idx=7, buffer=RingBuffer(1.0, FPS))
    with pytest.raises(ValueError, match="为空"):
        export_session(clip, tmp_path, writer=ClipWriter(fps=FPS))


def test_builtin_validation_catches_errors():
    good = build_session_json(
        session_id=new_session_id(),
        clip_dir=Path("/tmp/x"),
        left_video=Path("/tmp/x/left.mkv"),
        frame_count=30,
        fps=FPS,
        resolution=SIZE,
        video_sha256=False,
    )
    assert validate_session_builtin(good) == []

    bad = json.loads(json.dumps(good))
    bad["meta"]["session_id"] = "bad-id"
    bad["meta"]["duration_ms"] = -1
    bad["meta"]["handedness"] = "both"
    bad["video"]["resolution"] = [0, 0]
    errors = validate_session_builtin(bad)
    assert any("session_id" in e for e in errors)
    assert any("duration_ms" in e for e in errors)
    assert any("handedness" in e for e in errors)
    assert any("resolution" in e for e in errors)

    # pose2d 点序错乱被检出
    bad2 = json.loads(json.dumps(good))
    est = StubPoseEstimator()
    frame = est.estimate(None, 0, 0.0)  # type: ignore[arg-type]
    kps = frame.to_dict()["keypoints"]
    kps[0], kps[1] = kps[1], kps[0]
    bad2["pose2d"] = {"model": "m", "frame_rate": FPS,
                      "frames": [{"frame_index": 0, "timestamp_ms": 0, "confidence": 0.9, "keypoints": kps}]}
    errors2 = validate_session_builtin(bad2)
    assert any("keypoints" in e for e in errors2)


@pytest.mark.skipif(not core_validator_available(), reason="本机不存在 batana-core 仓")
def test_core_validate_session_full_pass(tmp_path):
    clip = make_clip()
    out_dir = export_session(
        clip, tmp_path, pose_frames=make_pose(clip),
        pose_model=StubPoseEstimator.model_name, writer=ClipWriter(fps=FPS),
    )
    errors = validate_with_core(out_dir / "session.json")
    assert errors == [], f"batana-core 全量校验失败: {errors}"


@pytest.mark.skipif(not core_validator_available(), reason="本机不存在 batana-core 仓")
def test_core_validate_catches_broken_session(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": "1.0", "meta": {}}), encoding="utf-8")
    errors = validate_with_core(bad)
    assert errors  # 全量校验必须报错
