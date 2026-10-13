"""app.recon 测试：双骨架三角化端到端（合成装置真参）、缺右目报错、合理性报告。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from app.pose import write_pose2d
from app.pose.model import KEYPOINT_NAMES, Keypoint, PoseFrame
from app.recon import load_pose_pair, reconstruct_session, sanity_report
from app.session import core_validator_available
from app.session.export import DEFAULT_CORE_REPO

CORE_SYNTHETIC_RIG = DEFAULT_CORE_REPO / "tests" / "synthetic_rig.py"
CORE_AVAILABLE = core_validator_available() and CORE_SYNTHETIC_RIG.is_file()
FPS = 30.0


def _load_rig_module():
    """按文件路径加载 core 仓 synthetic_rig（避免 sys.path 上 tests 包名冲突）。

    须先登记 sys.modules：dataclass 处理会按 __module__ 回查模块对象。
    """
    spec = importlib.util.spec_from_file_location("synthetic_rig", CORE_SYNTHETIC_RIG)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["synthetic_rig"] = mod
    spec.loader.exec_module(mod)
    return mod


def _humanoid_points_mm() -> np.ndarray:
    """33 点人体合理构型（站立，mm，左目坐标系 x 右 / y 下 / z 前），Z≈2000mm。"""
    raw = [
        (0, -1650),      # 0 nose
        (-15, -1660), (-30, -1660), (-45, -1660),   # 1-3 左眼
        (15, -1660), (30, -1660), (45, -1660),      # 4-6 右眼
        (-60, -1650), (60, -1650),                  # 7-8 耳
        (-15, -1620), (15, -1620),                  # 9-10 嘴
        (-200, -1450), (200, -1450),                # 11-12 肩
        (-230, -1150), (230, -1150),                # 13-14 肘
        (-240, -850), (240, -850),                  # 15-16 腕
        (-245, -820), (245, -820),                  # 17-18 小指
        (-248, -815), (248, -815),                  # 19-20 食指
        (-242, -825), (242, -825),                  # 21-22 拇指
        (-100, -950), (100, -950),                  # 23-24 髋
        (-105, -500), (105, -500),                  # 25-26 膝
        (-110, -80), (110, -80),                    # 27-28 踝
        (-115, -60), (115, -60),                    # 29-30 脚跟
        (-115, -30), (115, -30),                    # 31-32 脚尖
    ]
    assert len(raw) == 33
    # 缩放到 0.8 并下移 700mm，使人形落在画幅纵向范围内（y/z ∈ [-0.31, 0.34]）
    pts = np.array([[x * 0.8, y * 0.8 + 700.0, 2000.0] for x, y in raw])
    return pts


def _truth_calib_dict(rig) -> dict:
    """用 rig 真参构造标定 dict（对齐 calibrate_stereo 输出结构）。"""
    w, h = rig.left.size
    eye_l = {
        "resolution": [w, h],
        "intrinsic": rig.left.K.tolist(),
        "distortion": rig.left.d.tolist(),
    }
    eye_r = {
        "resolution": [w, h],
        "intrinsic": rig.right.K.tolist(),
        "distortion": rig.right.d.tolist(),
    }
    return {
        "format_version": "1.0",
        "stereo": {
            "baseline_mm": rig.baseline_mm,
            "left": eye_l,
            "right": eye_r,
            "extrinsic": {
                "rotation": rig.R_gt.tolist(),
                "translation_mm": rig.C_right.tolist(),
            },
        },
        "report": {"source": "calibrated"},
    }


def _write_fake_clip(clip_dir: Path, rig, n_frames: int = 5) -> list[np.ndarray]:
    """投影已知 3D 骨架成左右 2D，伪造双骨架 pose2d + capture_meta，返回真值帧列表。"""
    w, h = rig.left.size
    clip_dir.mkdir(parents=True, exist_ok=True)
    truth_frames = []
    for eye in ("left", "right"):
        frames = []
        for i in range(n_frames):
            # 轻微摆动（±几 mm）：骨长帧间应近乎不变
            sway = np.array([2.0 * i, 1.0 * i, 3.0 * i])
            pts = _humanoid_points_mm() + sway
            if eye == "left":
                truth_frames.append(pts)
            cam = rig.left if eye == "left" else rig.right
            uv = cam.project(pts)
            assert (uv[:, 0] >= 0).all() and (uv[:, 0] < w).all()
            assert (uv[:, 1] >= 0).all() and (uv[:, 1] < h).all()
            kps = [
                Keypoint(name=KEYPOINT_NAMES[k], x=float(uv[k, 0] / w),
                         y=float(uv[k, 1] / h), visibility=0.99)
                for k in range(33)
            ]
            frames.append(PoseFrame(frame_index=i, timestamp_ms=i / FPS * 1000.0,
                                    confidence=0.95, keypoints=kps))
        name = "pose2d.json" if eye == "left" else "pose2d_right.json"
        write_pose2d(clip_dir / name, "synthetic-rig", FPS, frames)
    (clip_dir / "capture_meta.json").write_text(json.dumps({
        "fps": FPS, "frame_count": n_frames, "per_eye_resolution": [w, h],
    }), encoding="utf-8")
    return truth_frames


@pytest.mark.skipif(not CORE_AVAILABLE, reason="本机不存在 batana-core 仓")
def test_reconstruct_session_e2e(tmp_path):
    """合成装置真参标定 + 投影 2D → 重建误差中位数 < 5mm，合理性报告链路合理。"""
    rig_mod = _load_rig_module()
    rig = rig_mod.VirtualStereoRig(baseline_mm=100.0, size=(1280, 800), seed=31)
    clip_dir = tmp_path / "clip"
    truth_frames = _write_fake_clip(clip_dir, rig)
    calib = _truth_calib_dict(rig)

    pose3d = reconstruct_session(clip_dir, calib, DEFAULT_CORE_REPO)
    assert (clip_dir / "pose3d.json").is_file()
    assert pose3d["unit"] == "m"
    assert pose3d["calibration_source"] == "calibrated"
    assert len(pose3d["frames"]) == len(truth_frames)

    errs = []
    for f, truth in zip(pose3d["frames"], truth_frames):
        est = np.array([[j["x"], j["y"], j["z"]] for j in f["joints"]]) * 1000.0
        assert [j["name"] for j in f["joints"]] == KEYPOINT_NAMES
        errs.append(np.linalg.norm(est - truth, axis=1))
    errs = np.concatenate(errs)
    median_mm = float(np.median(errs))
    print(f"\n重建误差 mm: 中位 {median_mm:.3f} / p95 {np.percentile(errs, 95):.3f}")
    assert median_mm < 5.0

    report = sanity_report(pose3d)
    print("\n".join(report["lines"]))
    assert report["verdict"] == "链路合理"
    assert report["issues"] == []


def test_load_pose_pair_missing_right(tmp_path):
    """缺右目骨架时抛 ValueError 并提示先跑右目骨架。"""
    clip_dir = tmp_path / "clip"
    clip_dir.mkdir()
    est_frames = [
        PoseFrame(frame_index=0, timestamp_ms=0.0, confidence=0.9,
                  keypoints=[Keypoint(name=n, x=0.5, y=0.5, visibility=0.9)
                             for n in KEYPOINT_NAMES])
    ]
    write_pose2d(clip_dir / "pose2d.json", "stub", FPS, est_frames)
    with pytest.raises(ValueError, match="右目"):
        load_pose_pair(clip_dir)


def test_sanity_report_flags_unstable_bones():
    """畸形数据（骨长剧烈波动 + 深度异常）必须报出异常项。"""

    def frame(scale: float, z: float) -> dict:
        joints = [
            {"name": n, "x": (i % 5) * 0.1 * scale, "y": (i // 5) * 0.2 * scale, "z": z,
             "visibility": 0.9}
            for i, n in enumerate(KEYPOINT_NAMES)
        ]
        return {"frame_index": 0, "t_ms": 0.0, "confidence": 0.9, "joints": joints}

    # 骨长逐帧剧烈缩放（变异系数远超 10%），深度中位 0.5m 低于合理区间
    pose3d = {
        "unit": "m",
        "calibration_source": "calibrated",
        "frames": [frame(1.0, 0.5), frame(2.0, 0.5), frame(0.5, 0.5)],
    }
    report = sanity_report(pose3d)
    assert report["verdict"] != "链路合理"
    assert any("骨长" in t for t in report["issues"])
    assert any("深度中位数" in t for t in report["issues"])
    assert report["lines"]


def test_sanity_report_nominal_source_noted():
    """名义标定来源在报告开头标注「仅供链路冒烟」。"""
    joints = [
        {"name": n, "x": (i % 5) * 0.1, "y": (i // 5) * 0.2, "z": 2.0, "visibility": 0.9}
        for i, n in enumerate(KEYPOINT_NAMES)
    ]
    pose3d = {
        "unit": "m",
        "calibration_source": "nominal-spec",
        "frames": [{"frame_index": i, "t_ms": i * 33.3, "confidence": 0.9,
                    "joints": joints} for i in range(3)],
    }
    report = sanity_report(pose3d)
    assert "名义标定" in report["lines"][0]
    assert report["verdict"] == "链路合理"
