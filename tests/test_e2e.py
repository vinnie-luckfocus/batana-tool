"""端到端：FileSource 读合成视频 → 检测 + 状态机 → 产出 ≥1 段素材目录并过校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.capture import ClipWriter, FileSource, RingBuffer, count_video_frames, split_sbs
from app.detect import CaptureStateMachine, Clip, PresenceDetector, SwingDetector
from app.pose import StubPoseEstimator
from app.session import (
    core_validator_available,
    export_session,
    validate_session_builtin,
    validate_with_core,
)
from app.voice import PROMPT_READY, PROMPT_SWING, NullVoice, prompt_saved
from samples.gen_synth import generate

FPS = 30.0
ROI = (80, 20, 170, 165)  # 覆盖 640x200 SBS（单目 320x200）中人形就位区域


@pytest.fixture(scope="module")
def synth_video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synth") / "synth.mkv"
    return generate(out, fps=FPS, width=640, height=200, cycles=2)


def run_pipeline(video: Path, out_root: Path) -> tuple[CaptureStateMachine, list[Path], NullVoice]:
    presence = PresenceDetector(
        ROI, FPS, ratio_thresh=0.02, stable_seconds=0.5, absent_seconds=0.7,
        learning_rate=0.002,
    )
    swing = SwingDetector(
        ROI, FPS, trigger_ratio=0.02, release_ratio=0.008,
        pre_roll_seconds=1.0, post_roll_seconds=0.8,
    )
    buffer = RingBuffer(3.0, FPS)
    voice = NullVoice()
    exported: list[Path] = []
    writer = ClipWriter(fps=FPS)
    estimator = StubPoseEstimator()

    def save_and_export(clip: Clip) -> None:
        frames = clip.frames()
        pose = [
            estimator.estimate(left, i, (frames[i][1] - frames[0][1]) / 1e6)
            for i, (_, _, left, _) in enumerate(frames)
        ]
        exported.append(
            export_session(
                clip, out_root, pose_frames=pose,
                pose_model=StubPoseEstimator.model_name, writer=writer,
            )
        )

    sm = CaptureStateMachine(
        presence, swing, buffer, FPS,
        voice=voice, clip_saver=save_and_export, countdown_seconds=1.0,
    )
    src = FileSource(video, fps=FPS)
    try:
        for idx, ts, sbs in src.frames():
            left, right = split_sbs(sbs)
            sm.feed_frame(idx, ts, left, right)
    finally:
        src.close()
    return sm, exported, voice


def test_e2e_produces_valid_sessions(synth_video, tmp_path):
    sm, exported, voice = run_pipeline(synth_video, tmp_path / "out")

    # 两个挥棒循环 → 至少 1 段素材（预期 2 段）
    assert len(exported) >= 1
    assert len(sm.clips) == len(exported)

    texts = voice.texts()
    assert PROMPT_READY in texts
    assert PROMPT_SWING in texts
    assert prompt_saved(1) in texts

    for out_dir in exported:
        names = {p.name for p in out_dir.iterdir()}
        assert {
            "session.json", "timestamps.csv", "capture_meta.json", "pose2d.json",
        } <= names
        session = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
        # 内置契约校验
        assert validate_session_builtin(session) == []
        # 视频帧数与时间戳一致
        clip_frame_count = session["pose2d"]["frames"].__len__()
        left_video = next(out_dir.glob("left.*"))
        assert count_video_frames(left_video) == clip_frame_count
        ts_lines = (out_dir / "timestamps.csv").read_text().strip().splitlines()
        assert len(ts_lines) == clip_frame_count + 1
        # 时长与帧率一致
        assert session["meta"]["duration_ms"] == round(clip_frame_count / FPS * 1000)


@pytest.mark.skipif(not core_validator_available(), reason="本机不存在 batana-core 仓")
def test_e2e_sessions_pass_core_full_validation(synth_video, tmp_path):
    _, exported, _ = run_pipeline(synth_video, tmp_path / "out")
    assert exported
    for out_dir in exported:
        errors = validate_with_core(out_dir / "session.json")
        assert errors == [], f"{out_dir.name} 全量校验失败: {errors}"
