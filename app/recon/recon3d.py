"""app.recon：3D 重建——双骨架 + 标定 → pose3d（契约结构）+ 无真值合理性报告。

全部无 Qt 依赖，可无头测试。三角化调用 batana-core 的
tools.recon3d.triangulate 批量模式（subprocess，模式同 validate_with_core）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from app.calib.store import SOURCE_NOMINAL
from app.pose.io import read_pose2d
from app.pose.model import KEYPOINT_NAMES, PoseFrame

_RECON_TIMEOUT_S = 300
# 整帧置信度低于该值的帧计入低置信统计（点仍保留，由消费方按 visibility 判断）
LOW_CONFIDENCE_THRESHOLD = 0.5

# 骨段表（索引即 KEYPOINT_NAMES 下标）
BONES: list[tuple[str, int, int]] = [
    ("shoulders", 11, 12),
    ("hips", 23, 24),
    ("left_upper_arm", 11, 13),
    ("left_forearm", 13, 15),
    ("right_upper_arm", 12, 14),
    ("right_forearm", 14, 16),
    ("left_torso", 11, 23),
    ("right_torso", 12, 24),
    ("left_thigh", 23, 25),
    ("left_shank", 25, 27),
    ("right_thigh", 24, 26),
    ("right_shank", 26, 28),
]

# 合理性判据阈值
_BONE_CV_MAX = 0.10       # 骨长帧间变异系数上限
_DEPTH_POSITIVE_MIN = 0.95  # Z>0 比例下限
_DEPTH_MEDIAN_RANGE = (1.0, 6.0)  # 深度中位数合理区间（米）


def load_pose_pair(
    clip_dir: str | Path,
) -> tuple[str, float, list[PoseFrame], list[PoseFrame]]:
    """读取双骨架 pose2d.json + pose2d_right.json，返回 (model, fps, 左目帧, 右目帧)。"""
    clip = Path(clip_dir)
    left_path = clip / "pose2d.json"
    right_path = clip / "pose2d_right.json"
    if not left_path.is_file():
        raise ValueError(f"左目骨架缺失: {left_path}（先在审核页跑骨架）")
    if not right_path.is_file():
        raise ValueError(f"右目骨架缺失: {right_path}（先在审核页对右目跑骨架）")
    model_l, fps, left_frames = read_pose2d(left_path)
    _model_r, _fps_r, right_frames = read_pose2d(right_path)
    return model_l, fps, left_frames, right_frames


def _read_per_eye_resolution(clip: Path) -> tuple[int, int]:
    meta_path = clip / "capture_meta.json"
    if not meta_path.is_file():
        raise ValueError(f"片段缺少 capture_meta.json: {clip}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    res = meta.get("per_eye_resolution")
    if not (isinstance(res, list) and len(res) == 2):
        raise ValueError(f"capture_meta.json 缺 per_eye_resolution: {clip}")
    return int(res[0]), int(res[1])


def _to_pixel_frames(
    left_frames: list[PoseFrame],
    right_frames: list[PoseFrame],
    width: int,
    height: int,
) -> tuple[list[dict], int]:
    """归一化关键点还原为像素坐标，组装批量输入帧；返回 (frames, 低置信帧数)。"""
    right_by_index = {f.frame_index: f for f in right_frames}
    batch: list[dict] = []
    low_conf = 0
    for lf in left_frames:
        rf = right_by_index.get(lf.frame_index)
        if rf is None:
            continue  # 右目缺该帧则跳过（不成对无法三角化）
        if lf.confidence < LOW_CONFIDENCE_THRESHOLD or rf.confidence < LOW_CONFIDENCE_THRESHOLD:
            low_conf += 1
        batch.append({
            "frame_index": lf.frame_index,
            "timestamp_ms": lf.timestamp_ms,
            "left": [[kp.x * width, kp.y * height] for kp in lf.keypoints],
            "right": [[kp.x * width, kp.y * height] for kp in rf.keypoints],
        })
    return batch, low_conf


def reconstruct_session(
    clip_dir: str | Path,
    calib: dict,
    core_repo: str | Path,
    out_path: str | Path | None = None,
) -> dict:
    """双骨架 + 标定 → pose3d 契约结构（左目坐标系，单位米），写 pose3d.json 并返回。

    calib 为标定 dict（名义或实测均可，report.source 标明来源）；
    core_repo 为 batana-core 仓路径（三角化在其环境下跑）。
    """
    clip = Path(clip_dir)
    width, height = _read_per_eye_resolution(clip)
    model, fps, left_frames, right_frames = load_pose_pair(clip)
    batch, low_conf = _to_pixel_frames(left_frames, right_frames, width, height)
    if not batch:
        raise ValueError(f"双骨架无共视帧，无法三角化: {clip}")

    core = Path(core_repo)
    if not (core / "tools" / "recon3d" / "triangulate.py").is_file():
        raise ValueError(f"batana-core 仓不可用（缺 triangulate）: {core}")

    with tempfile.TemporaryDirectory(prefix="batana-recon-") as tmp:
        tmp_dir = Path(tmp)
        calib_path = tmp_dir / "calib.json"
        batch_path = tmp_dir / "batch.json"
        out_json = tmp_dir / "out.json"
        calib_path.write_text(json.dumps(calib, ensure_ascii=False), encoding="utf-8")
        batch_path.write_text(json.dumps({"frames": batch}, ensure_ascii=False), encoding="utf-8")
        cmd = [
            sys.executable, "-m", "tools.recon3d.triangulate",
            "--calib", str(calib_path),
            "--batch", str(batch_path),
            "--out", str(out_json),
        ]
        try:
            proc = subprocess.run(cmd, cwd=core, capture_output=True, text=True,
                                  timeout=_RECON_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ValueError(f"三角化超时（>{_RECON_TIMEOUT_S}s）: {clip}") from None
        if proc.returncode != 0 or not out_json.is_file():
            detail = "\n".join(
                line for line in (proc.stdout, proc.stderr) if line and line.strip()
            )
            tail = "\n".join(detail.splitlines()[-12:])
            raise ValueError(f"core 三角化失败（退出码 {proc.returncode}）:\n{tail}")
        result = json.loads(out_json.read_text(encoding="utf-8"))

    # mm → m，组装契约 pose3d（t_ms/joints 为契约字段，其余为允许的新增元信息）
    source = (calib.get("report") or {}).get("source", "calibrated")
    calibration_source = SOURCE_NOMINAL if source == SOURCE_NOMINAL else "calibrated"
    left_conf = {f.frame_index: f.confidence for f in left_frames}
    vis_by_frame = {f.frame_index: [kp.visibility for kp in f.keypoints]
                    for f in left_frames}
    frames_out: list[dict] = []
    for f in result["frames"]:
        fi = f["frame_index"]
        pts = np.asarray(f["points_3d"], dtype=np.float64) / 1000.0
        vis = vis_by_frame.get(fi, [1.0] * len(pts))
        joints = [
            {
                "name": KEYPOINT_NAMES[i] if i < len(KEYPOINT_NAMES) else f"kp_{i}",
                "x": float(p[0]), "y": float(p[1]), "z": float(p[2]),
                "visibility": float(vis[i]) if i < len(vis) else 1.0,
            }
            for i, p in enumerate(pts)
        ]
        ts = float(f["timestamp_ms"])
        frames_out.append({
            "frame_index": int(fi),
            "timestamp_ms": ts,
            "t_ms": ts,
            "confidence": float(left_conf.get(fi, 1.0)),
            "joints": joints,
        })

    pose3d = {
        "model": model,
        "frame_rate": fps,
        "unit": "m",
        "frame": "left_camera",
        "calibration_source": calibration_source,
        "calibration_baseline_mm": (calib.get("stereo") or {}).get("baseline_mm"),
        "frames": frames_out,
        "report": {
            "frame_count": len(frames_out),
            "low_confidence_frames": low_conf,
        },
    }
    out = Path(out_path) if out_path else clip / "pose3d.json"
    out.write_text(json.dumps(pose3d, ensure_ascii=False, indent=2), encoding="utf-8")
    return pose3d


def sanity_report(pose3d: dict) -> dict:
    """无真值合理性判据：骨长稳定性 + 深度分布 + 低置信占比 → verdict 与中文报告行。"""
    frames = pose3d.get("frames") or []
    issues: list[str] = []
    lines: list[str] = []

    # ---- 骨长统计 ----
    bones: dict[str, dict] = {}
    for name, i, j in BONES:
        lengths = []
        for f in frames:
            joints = f.get("joints") or []
            if len(joints) <= max(i, j):
                continue
            a, b = joints[i], joints[j]
            lengths.append(float(np.linalg.norm(
                [a["x"] - b["x"], a["y"] - b["y"], a["z"] - b["z"]]
            )))
        if not lengths:
            bones[name] = {"frames": 0}
            continue
        arr = np.asarray(lengths)
        mean = float(arr.mean())
        std = float(arr.std())
        cv = std / mean if mean > 1e-9 else float("inf")
        bones[name] = {
            "frames": len(lengths),
            "mean_m": mean,
            "std_m": std,
            "cv": cv,
        }
    unstable = [n for n, s in bones.items() if s.get("cv", 0.0) >= _BONE_CV_MAX]
    if unstable:
        issues.append(f"骨长帧间变异系数 ≥{_BONE_CV_MAX:.0%}: {', '.join(unstable)}")

    # ---- 深度分布 ----
    zs = np.asarray([
        kp["z"] for f in frames for kp in (f.get("joints") or [])
    ], dtype=np.float64)
    if zs.size:
        p5, p50, p95 = (float(np.percentile(zs, q)) for q in (5, 50, 95))
        ratio_pos = float((zs > 0).mean())
    else:
        p5 = p50 = p95 = 0.0
        ratio_pos = 0.0
        issues.append("无任何 3D 点")
    depth = {"p5_m": p5, "p50_m": p50, "p95_m": p95, "z_positive_ratio": ratio_pos}
    if zs.size and ratio_pos < _DEPTH_POSITIVE_MIN:
        issues.append(f"Z>0 比例 {ratio_pos:.1%} 低于 {_DEPTH_POSITIVE_MIN:.0%}（相机后方点过多）")
    if zs.size and not (_DEPTH_MEDIAN_RANGE[0] <= p50 <= _DEPTH_MEDIAN_RANGE[1]):
        lo, hi = _DEPTH_MEDIAN_RANGE
        issues.append(f"深度中位数 {p50:.2f}m 超出合理区间 [{lo:.0f}, {hi:.0f}]m")

    # ---- 低置信帧占比 ----
    n_frames = len(frames)
    low = sum(1 for f in frames if f.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD)
    low_ratio = low / n_frames if n_frames else 0.0

    verdict = "链路合理" if not issues else "存在异常"

    # ---- 中文报告行 ----
    source = pose3d.get("calibration_source", "calibrated")
    if source == SOURCE_NOMINAL:
        lines.append("标定来源：名义标定，仅供链路冒烟（不能用于精度结论）")
    else:
        baseline = pose3d.get("calibration_baseline_mm")
        suffix = f"，基线 {baseline:.1f}mm" if isinstance(baseline, (int, float)) else ""
        lines.append(f"标定来源：实测标定{suffix}")
    lines.append(f"帧数：{n_frames}（低置信帧 {low}，占比 {low_ratio:.1%}）")
    lines.append(f"深度 Z（米）：p5 {p5:.2f} / 中位 {p50:.2f} / p95 {p95:.2f}，"
                 f"Z>0 比例 {ratio_pos:.1%}")
    lines.append("骨长（均值 m / 帧间变异系数）：")
    for name, _i, _j in BONES:
        s = bones.get(name) or {}
        if "mean_m" in s:
            lines.append(f"  {name}: {s['mean_m']:.3f} m / {s['cv']:.1%}（{s['frames']} 帧）")
        else:
            lines.append(f"  {name}: 无数据")
    lines.append(f"结论：{verdict}")
    if issues:
        lines.append("异常项：")
        lines.extend(f"  - {t}" for t in issues)

    return {
        "bones": bones,
        "depth": depth,
        "low_confidence_frames": low,
        "low_confidence_ratio": low_ratio,
        "verdict": verdict,
        "issues": issues,
        "lines": lines,
    }
