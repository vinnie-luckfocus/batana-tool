"""pose2d.json 读写往返、手动修正保存/重载保持。"""

from __future__ import annotations

import pytest

from app.pose import (
    KEYPOINT_NAMES,
    Keypoint,
    PoseFrame,
    StubPoseEstimator,
    read_pose2d,
    write_pose2d,
)


def make_frames(count: int) -> list[PoseFrame]:
    est = StubPoseEstimator()
    return [est.estimate(None, i, i * 33.3) for i in range(count)]  # type: ignore[arg-type]


def test_pose_frame_requires_33_keypoints():
    with pytest.raises(ValueError, match="33"):
        PoseFrame(frame_index=0, timestamp_ms=0.0, confidence=0.5,
                  keypoints=[Keypoint("nose", 0.5, 0.5, 0.9)])


def test_stub_estimator_deterministic():
    a, b = make_frames(3), make_frames(3)
    for fa, fb in zip(a, b):
        assert [(k.x, k.y, k.visibility) for k in fa.keypoints] == [
            (k.x, k.y, k.visibility) for k in fb.keypoints
        ]
    assert len(a[0].keypoints) == 33
    assert [k.name for k in a[0].keypoints] == KEYPOINT_NAMES
    assert all(0.0 <= k.x <= 1.0 and 0.0 <= k.y <= 1.0 for k in a[0].keypoints)


def test_write_read_roundtrip(tmp_path):
    frames = make_frames(4)
    path = write_pose2d(tmp_path / "pose2d.json", "batana-pose-stub-v0.0", 120.0, frames)
    model, rate, loaded = read_pose2d(path)
    assert model == "batana-pose-stub-v0.0"
    assert rate == 120.0
    assert len(loaded) == 4
    for orig, back in zip(frames, loaded):
        assert orig.to_dict() == back.to_dict()


def test_manual_correction_preserved_after_reload(tmp_path):
    frames = make_frames(2)
    target = frames[1]
    auto_before = (target.keypoints[0].x, target.keypoints[0].y, target.keypoints[0].visibility)
    target.correct("nose", 0.11, 0.22)
    kp = target.keypoints[0]
    assert kp.manual is True
    assert kp.x == 0.11 and kp.y == 0.22
    assert kp.auto == {"x": auto_before[0], "y": auto_before[1], "visibility": auto_before[2]}

    path = write_pose2d(tmp_path / "pose2d.json", "m", 30.0, frames)
    _, _, loaded = read_pose2d(path)
    kp2 = loaded[1].keypoints[0]
    assert kp2.manual is True
    assert kp2.x == 0.11 and kp2.y == 0.22
    assert kp2.auto == kp.auto
    # 未修正的点不带 manual/auto 字段
    assert loaded[1].keypoints[1].manual is False
    assert loaded[1].keypoints[1].auto is None


def test_repeated_correction_keeps_first_auto_value():
    frame = make_frames(1)[0]
    first_auto = (frame.keypoints[5].x, frame.keypoints[5].y)
    frame.correct(5, 0.3, 0.3)
    frame.correct(5, 0.4, 0.4)
    kp = frame.keypoints[5]
    assert kp.x == 0.4
    assert (kp.auto["x"], kp.auto["y"]) == first_auto  # 保留最早自动原值


def test_mediapipe_estimator_graceful_degradation(tmp_path):
    from app.pose import MediaPipePoseEstimator

    if not MediaPipePoseEstimator.is_available():
        pytest.skip("mediapipe 不可用（优雅降级路径）")
    with pytest.raises(FileNotFoundError, match="模型文件不存在"):
        MediaPipePoseEstimator(tmp_path / "nonexistent.task")
