"""app.calib 测试：名义标定结构、标定存档、抽帧、core 标定端到端（合成装置）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import cv2
import pytest

from app.calib.frames import extract_frame_pairs
from app.calib.nominal import nominal_calibration
from app.calib.runner import run_core_calibration
from app.calib.store import (
    calibration_status,
    load_calibration,
    save_calibration,
)
from app.capture import ClipWriter
from app.session import core_validator_available
from app.session.export import DEFAULT_CORE_REPO

CORE_SYNTHETIC_RIG = DEFAULT_CORE_REPO / "tests" / "synthetic_rig.py"
CORE_AVAILABLE = core_validator_available() and CORE_SYNTHETIC_RIG.is_file()


def _load_core_module(name: str, relpath: str):
    """按文件路径加载 core 仓模块（避免 sys.path 上 tests 包名冲突）。

    须先登记 sys.modules：dataclass 处理会按 __module__ 回查模块对象。
    """
    import sys

    spec = importlib.util.spec_from_file_location(name, DEFAULT_CORE_REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- 名义标定 ----


def test_nominal_calibration_matches_contract():
    calib = nominal_calibration(1280, 720)
    assert calib["format_version"] == "1.0"
    stereo = calib["stereo"]
    assert stereo["baseline_mm"] == 120.0
    for eye in ("left", "right"):
        assert stereo[eye]["resolution"] == [1280, 720]
        K = stereo[eye]["intrinsic"]
        assert len(K) == 3 and all(len(row) == 3 for row in K)
        assert K[0][0] == pytest.approx(1200.0)  # f 3.6mm / 像元 3µm
        assert K[0][2] == pytest.approx(639.5)   # 主点 w/2-0.5
        assert K[1][2] == pytest.approx(359.5)
        assert stereo[eye]["distortion"] == [0.0] * 5
    ext = stereo["extrinsic"]
    assert ext["rotation"] == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    assert ext["translation_mm"] == [120.0, 0.0, 0.0]
    assert calib["report"]["source"] == "nominal-spec"
    assert "估算" in calib["report"]["basis"]


def test_nominal_calibration_scales_with_resolution():
    calib = nominal_calibration(640, 400)
    K = calib["stereo"]["left"]["intrinsic"]
    assert K[0][0] == pytest.approx(600.0)  # 1200 × 640/1280
    assert K[0][2] == pytest.approx(319.5)


# ---- 标定存档 ----


def test_store_roundtrip_and_status(tmp_path):
    path = tmp_path / "calibration.json"
    assert load_calibration(path) is None
    assert calibration_status(path) == "无标定"

    nominal = nominal_calibration(1280, 720)
    save_calibration(nominal, path)
    loaded = load_calibration(path)
    assert loaded["stereo"]["baseline_mm"] == 120.0
    status = calibration_status(path)
    assert "名义标定" in status

    measured = json.loads(json.dumps(nominal))
    measured["report"] = {
        "source": "calibrated",
        "calibrated_at": "2026-10-13T02:30:00Z",
        "reprojection_error_px": {"left": 0.21, "right": 0.23, "stereo": 0.34},
    }
    save_calibration(measured, path)
    status = calibration_status(path)
    assert "实测标定" in status
    assert "2026-10-13" in status
    assert "0.340px" in status
    assert "120.0mm" in status

    # 损坏文件按无标定处理
    path.write_text("{broken", encoding="utf-8")
    assert load_calibration(path) is None


# ---- 抽帧 ----


def _make_clip_dir(tmp_path: Path, fps: float = 10.0) -> Path:
    """用 gen_synth 造合成 SBS 视频，切半后按片段目录结构落盘。"""
    from samples.gen_synth import generate

    sbs = generate(
        tmp_path / "synth.mkv", fps=fps, width=320, height=200, cycles=1,
        empty_s=0.2, walkin_s=0.6, swing_s=0.3, still_s=0.2,
    )
    cap = cv2.VideoCapture(str(sbs))
    items = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame.ndim == 3:  # FFmpeg 读回为 BGR，转回灰度对齐采集帧格式
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        half = frame.shape[1] // 2
        items.append((idx, int(idx / fps * 1e9), frame[:, :half], frame[:, half:]))
        idx += 1
    cap.release()
    assert items
    clip_dir = tmp_path / "clip"
    ClipWriter(fps=fps).write_clip(items, clip_dir)
    return clip_dir


def test_extract_frame_pairs(tmp_path):
    clip_dir = _make_clip_dir(tmp_path)
    out_dir = tmp_path / "frames"
    n = extract_frame_pairs(clip_dir, out_dir, max_frames=5)
    assert 1 <= n <= 5
    lefts = sorted(out_dir.glob("left_*.png"))
    rights = sorted(out_dir.glob("right_*.png"))
    assert len(lefts) == len(rights) == n
    img = cv2.imread(str(lefts[0]), cv2.IMREAD_GRAYSCALE)
    assert img is not None and img.shape == (200, 160)


def test_extract_frame_pairs_missing_video(tmp_path):
    with pytest.raises(ValueError, match="缺少 left"):
        extract_frame_pairs(tmp_path / "nope", tmp_path / "out")


# ---- core 标定端到端（合成装置，须本机存在 batana-core 仓）----


@pytest.mark.skipif(not CORE_AVAILABLE, reason="本机不存在 batana-core 仓")
def test_run_core_calibration_recovers_baseline(tmp_path):
    """VirtualStereoRig 渲染共视 ChArUco 帧 → core 标定 → 基线/RMS 在容差内。"""
    rig_mod = _load_core_module("synthetic_rig", "tests/synthetic_rig.py")
    board_mod = _load_core_module("batana_core_board", "tools/calibration/board.py")
    rig = rig_mod.VirtualStereoRig(baseline_mm=100.0, size=(1280, 800), seed=21)
    board = board_mod.make_board()  # 11×8，格子 100mm（与 runner 默认参数一致）

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    n = 0
    while n < 8:
        rvec, tvec = rig.sample_board_pose()
        if not rig.board_fully_visible(board, rvec, tvec):
            continue
        img_l = rig.render_board(board, rvec, tvec, rig.left)
        img_r = rig.render_board(board, rvec, tvec, rig.right)
        cv2.imwrite(str(frames_dir / f"left_{n:04d}.png"), img_l)
        cv2.imwrite(str(frames_dir / f"right_{n:04d}.png"), img_r)
        n += 1

    data = run_core_calibration(frames_dir, DEFAULT_CORE_REPO, tmp_path / "calib.json")
    baseline = data["stereo"]["baseline_mm"]
    rms = data["report"]["reprojection_error_px"]["stereo"]
    print(f"\n标定恢复：基线 {baseline:.2f}mm（真值 100mm），立体 RMS {rms:.4f}px")
    assert abs(baseline - 100.0) <= 5.0  # ±5%
    assert rms < 0.5
    assert data["report"]["source"] == "calibrated"
    assert data["report"]["calibrated_at"]
