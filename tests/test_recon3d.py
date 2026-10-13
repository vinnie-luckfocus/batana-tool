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


def _humanoid_joints_m() -> list[dict]:
    """33 关节 dict（米制，z≈2.0m），供 sanity_report 直接构造 pose3d。"""
    pts = _humanoid_points_mm()
    return [
        {"name": KEYPOINT_NAMES[k], "x": float(pts[k, 0]) / 1000.0,
         "y": float(pts[k, 1]) / 1000.0, "z": float(pts[k, 2]) / 1000.0,
         "visibility": 0.9}
        for k in range(33)
    ]


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
    assert report["verdict"] == "链路合理（精度 A）"  # 干净合成数据：全部骨段鲁棒 CV≈0
    assert report["grade"] == "A"
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
    """链路异常案例：深度发散（中位 0.5m 低于合理区间）必须判「链路异常」。"""

    def frame(scale: float, z: float) -> dict:
        joints = [
            {"name": n, "x": (i % 5) * 0.1 * scale, "y": (i // 5) * 0.2 * scale, "z": z,
             "visibility": 0.9}
            for i, n in enumerate(KEYPOINT_NAMES)
        ]
        return {"frame_index": 0, "t_ms": 0.0, "confidence": 0.9, "joints": joints}

    pose3d = {
        "unit": "m",
        "calibration_source": "calibrated",
        "frames": [frame(1.0, 0.5), frame(2.0, 0.5), frame(0.5, 0.5)],
    }
    report = sanity_report(pose3d)
    assert report["verdict"] == "链路异常"
    assert report["grade"] is None
    assert any("深度中位数" in t for t in report["issues"])
    assert report["lines"]


def test_sanity_report_flags_implausible_bone_medians():
    """链路异常案例：骨长中位离谱（肩宽 2m，非人类）→ 判「链路异常」并列出具体段。"""
    frames = []
    for i in range(5):
        joints = _humanoid_joints_m()
        joints[11] = {**joints[11], "x": -1.0}  # 左肩拉到 -1m → 肩宽 2m
        joints[12] = {**joints[12], "x": 1.0}
        frames.append({"frame_index": i, "t_ms": i * 33.3, "confidence": 0.9,
                       "joints": joints})
    pose3d = {"unit": "m", "calibration_source": "calibrated", "frames": frames}
    report = sanity_report(pose3d)
    assert report["verdict"] == "链路异常"
    assert any("骨长中位" in t and "shoulders" in t for t in report["issues"])


def test_sanity_report_nominal_source_noted():
    """名义标定来源在报告开头标注「仅供链路冒烟」。"""
    joints = _humanoid_joints_m()
    pose3d = {
        "unit": "m",
        "calibration_source": "nominal-spec",
        "frames": [{"frame_index": i, "t_ms": i * 33.3, "confidence": 0.9,
                    "joints": joints} for i in range(3)],
    }
    report = sanity_report(pose3d)
    assert "名义标定" in report["lines"][0]
    assert report["verdict"] == "链路合理（精度 A）"


# ---- 掩蔽与鲁棒统计（2026-10-13：MediaPipe 抖动修复）----


def _joints_with_mask(scale: float = 1.0, masked: set[int] | None = None) -> list[dict]:
    """人形关节（米制）× 缩放，支持掩蔽指定下标。骨长中位须落在人体合理区间。"""
    joints = []
    for i, j in enumerate(_humanoid_joints_m()):
        if masked and i in masked:
            joints.append({"name": j["name"], "x": None, "y": None, "z": None,
                           "visibility": 0.0})
        else:
            joints.append({**j, "x": j["x"] * scale, "y": j["y"] * scale,
                           "z": j["z"] * scale})
    return joints


def test_sanity_report_skips_masked_joints():
    """掩蔽关节（坐标 null）不参与骨长/深度统计；掩蔽过多的段单独提示。"""
    frames = []
    for i in range(10):
        # 左肘(13)、左腕(15) 在前 6 帧掩蔽 → left_upper_arm/forearm 有效帧占比 40%
        masked = {13, 15} if i < 6 else set()
        frames.append({"frame_index": i, "t_ms": i * 33.3, "confidence": 0.9,
                       "joints": _joints_with_mask(masked=masked)})
    pose3d = {"unit": "m", "calibration_source": "calibrated", "frames": frames}
    report = sanity_report(pose3d)
    # 掩蔽段有效帧占比 40% < 50% → 掩蔽过多告警（数据完整性，不影响链路判定）
    assert any("掩蔽过多" in t for t in report["warnings"])
    assert "left_upper_arm" in next(t for t in report["warnings"] if "掩蔽过多" in t)
    # 未掩蔽段统计正常（valid_ratio 1.0，鲁棒 CV 0）
    assert report["bones"]["shoulders"]["valid_ratio"] == 1.0
    assert report["bones"]["shoulders"]["robust_cv"] == 0.0
    # 有效帧占比不足的段不计入稳定数；2 段掩蔽过多 → 无法评 A，长骨全稳 → B
    assert report["stable_bones"] == 10
    assert report["verdict"] == "链路合理（精度 B）"


def test_sanity_report_robust_to_outlier_frames():
    """鲁棒口径：个别离群帧不再拉高骨长变异（MAD÷中位 vs 旧 std÷均值）。"""
    frames = []
    for i in range(20):
        scale = 3.0 if i == 10 else 1.0  # 单帧整体放大 3 倍（旧口径 CV 必然爆表）
        frames.append({"frame_index": i, "t_ms": i * 33.3, "confidence": 0.9,
                       "joints": _joints_with_mask(scale=scale)})
    pose3d = {"unit": "m", "calibration_source": "calibrated", "frames": frames}
    report = sanity_report(pose3d)
    assert report["bones"]["shoulders"]["robust_cv"] < 0.10
    assert report["verdict"] == "链路合理（精度 A）"
    # 对照：旧口径 std/mean 会被离群帧推高（肩宽 0.32m × 单帧 3 倍野值）
    import numpy as np
    lengths = np.array([0.32 * (3.0 if i == 10 else 1.0) for i in range(20)])
    assert lengths.std() / lengths.mean() > 0.10  # 旧口径报异常，鲁棒口径不误报


@pytest.mark.skipif(not CORE_AVAILABLE, reason="本机不存在 batana-core 仓")
def test_reconstruct_session_masks_low_visibility(tmp_path):
    """低 visibility 关键点在 pose3d 中坐标为 null、visibility=0（契约缺失点写法）。"""
    rig_mod = _load_rig_module()
    rig = rig_mod.VirtualStereoRig(baseline_mm=100.0, size=(1280, 800), seed=41)
    clip_dir = tmp_path / "clip"
    _write_fake_clip(clip_dir, rig, n_frames=3)
    # 把左目帧 1 的左腕(15) visibility 压到阈值下（模拟遮挡/丢失）
    from app.pose import read_pose2d, write_pose2d as wp
    model, fps, frames = read_pose2d(clip_dir / "pose2d.json")
    frames[1].keypoints[15].visibility = 0.2
    wp(clip_dir / "pose2d.json", model, fps, frames)

    calib = _truth_calib_dict(rig)
    pose3d = reconstruct_session(clip_dir, calib, DEFAULT_CORE_REPO)
    joint = pose3d["frames"][1]["joints"][15]
    assert joint["x"] is None and joint["y"] is None and joint["z"] is None
    assert joint["visibility"] == 0.0
    assert joint["name"] == "left_wrist"
    # 相邻帧同点不受影响
    assert pose3d["frames"][0]["joints"][15]["x"] is not None
    # 报告能处理含 null 关节的数据
    report = sanity_report(pose3d)
    assert report["bones"]["left_forearm"]["frames"] == 2  # 帧 1 被跳过
