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

# 合理性判据阈值（两维口径：链路判定 + 精度等级）
_MIN_VALID_RATIO = 0.5    # 骨段有效帧占比下限（低于此值该段不参与评级，报告中提示掩蔽过多）
_DEPTH_POSITIVE_MIN = 0.95  # Z>0 比例下限（链路判定）
_DEPTH_MEDIAN_RANGE = (1.0, 6.0)  # 深度中位数合理区间（米，链路判定）
_MIN_IN_RANGE_BONES = 8   # 链路判定：中位骨长落在人体合理区间的段数下限（12 段的 2/3）
_GRADE_A_CV = 0.10        # 精度 A：全部 12 段鲁棒 CV 上限
_GRADE_B_LONG_CV = 0.15   # 精度 B：6 段长骨（torso/thigh/shank）鲁棒 CV 上限

# 长骨段（精度 B 的考察对象）
LONG_BONES = [
    "left_torso", "right_torso",
    "left_thigh", "right_thigh",
    "left_shank", "right_shank",
]

# 骨长人体合理区间（米）。来源：常见人体测量学的近似范围（成人肩宽/骨盆宽/
# 大臂/前臂/躯干/大腿/小腿），仅用于链路判定（检出系统性错误），不作精度结论。
BONE_LENGTH_RANGE_M = {
    "shoulders": (0.30, 0.60),
    "hips": (0.20, 0.45),
    "left_upper_arm": (0.20, 0.50),
    "left_forearm": (0.20, 0.50),
    "right_upper_arm": (0.20, 0.50),
    "right_forearm": (0.20, 0.50),
    "left_torso": (0.35, 0.70),
    "right_torso": (0.35, 0.70),
    "left_thigh": (0.30, 0.60),
    "left_shank": (0.30, 0.60),
    "right_thigh": (0.30, 0.60),
    "right_shank": (0.30, 0.60),
}

# 精度 C 的改进建议（数据噪声下限说明 + 采集/标定升级路径）
GRADE_C_ADVICE = (
    "骨长波动主要来自双目 2D 抖动（当前分辨率下 ~1px 视差噪声 ≈ ±80mm 深度），"
    "建议：1280×720 高档位采集 + 标定板实测标定后可显著改善"
)


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
    """归一化关键点还原为像素坐标，组装批量输入帧（带双目 visibility）；返回 (frames, 低置信帧数)。"""
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
            "left_visibility": [kp.visibility for kp in lf.keypoints],
            "right_visibility": [kp.visibility for kp in rf.keypoints],
        })
    return batch, low_conf


def reconstruct_session(
    clip_dir: str | Path,
    calib: dict,
    core_repo: str | Path,
    out_path: str | Path | None = None,
    min_vis: float = 0.5,
    smooth: int = 5,
    epipolar_correct: bool = True,
) -> dict:
    """双骨架 + 标定 → pose3d 契约结构（左目坐标系，单位米），写 pose3d.json 并返回。

    calib 为标定 dict（名义或实测均可，report.source 标明来源）；
    core_repo 为 batana-core 仓路径（三角化在其环境下跑）。
    前处理默认：min-vis 0.5 掩蔽 + 5 帧时序中值滤波 + 极线校正——
    MediaPipe 逐帧抖动与双目对应点不一致会被裸三角化放大成骨长剧烈波动。
    被掩蔽关节在 pose3d 中写 visibility=0 且坐标为 null（契约缺失点约定）。
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
            "--min-vis", str(min_vis),
            "--smooth", str(smooth),
        ]
        if epipolar_correct:
            cmd.append("--epipolar-correct")
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
        raw_pts = f["points_3d"]
        vis = vis_by_frame.get(fi, [1.0] * len(raw_pts))
        joints = []
        for i, p in enumerate(raw_pts):
            name = KEYPOINT_NAMES[i] if i < len(KEYPOINT_NAMES) else f"kp_{i}"
            if p is None:
                # 被掩蔽（低 visibility 未参与三角化）：契约缺失点写法
                joints.append({"name": name, "x": None, "y": None, "z": None,
                               "visibility": 0.0})
            else:
                joints.append({
                    "name": name,
                    "x": float(p[0]) / 1000.0,   # mm → m
                    "y": float(p[1]) / 1000.0,
                    "z": float(p[2]) / 1000.0,
                    "visibility": float(vis[i]) if i < len(vis) else 1.0,
                })
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


def _joint_ok(j: dict) -> bool:
    """关节有效（未被掩蔽）：坐标非 null。"""
    return j.get("x") is not None and j.get("z") is not None


def sanity_report(pose3d: dict) -> dict:
    """无真值合理性判据：「链路判定 + 精度等级」两维结论。

    掩蔽关节（坐标 null）不参与统计：骨长只计入两端点均有效的帧，
    深度统计只取有效点。鲁棒 CV = MAD ÷ 中位数（对离群帧不敏感）。

    链路判定（任一不满足 → verdict「链路异常」）：
    Z>0 比例 ≥95%、深度中位 1–6m、≥2/3 骨段中位长度落在人体合理区间
    （BONE_LENGTH_RANGE_M，近似人体测量学范围，检出系统性错误而非噪声）。
    精度等级（链路合理时分级，基于鲁棒 CV）：A = 全部 12 段 <10%；
    B = 6 段长骨（torso/thigh/shank）全部 <15%；C = 其余（附改进建议）。
    """
    frames = pose3d.get("frames") or []
    issues: list[str] = []
    lines: list[str] = []
    n_frames = len(frames)

    # ---- 骨长统计（鲁棒：中位数 + MAD/中位；只计两端点均未掩蔽的帧）----
    bones: dict[str, dict] = {}
    for name, i, j in BONES:
        lengths = []
        for f in frames:
            joints = f.get("joints") or []
            if len(joints) <= max(i, j):
                continue
            a, b = joints[i], joints[j]
            if not (_joint_ok(a) and _joint_ok(b)):
                continue
            lengths.append(float(np.linalg.norm(
                [a["x"] - b["x"], a["y"] - b["y"], a["z"] - b["z"]]
            )))
        valid_ratio = len(lengths) / n_frames if n_frames else 0.0
        if not lengths:
            bones[name] = {"frames": 0, "valid_ratio": 0.0}
            continue
        arr = np.asarray(lengths)
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median)))
        rcv = mad / median if median > 1e-9 else float("inf")
        bones[name] = {
            "frames": len(lengths),
            "valid_ratio": valid_ratio,
            "median_m": median,
            "mad_m": mad,
            "robust_cv": rcv,
        }

    # ---- 掩蔽过多的段单独提示（数据完整性告警，不影响链路判定）----
    sparse = [n for n, s in bones.items() if s.get("valid_ratio", 0.0) < _MIN_VALID_RATIO]
    warnings: list[str] = []
    if sparse:
        warnings.append(
            f"掩蔽过多（有效帧占比 <{_MIN_VALID_RATIO:.0%}，不参与评级）: {', '.join(sparse)}"
        )

    # ---- 链路判定 1/3：骨长中位落在人体合理区间的段数 ----
    in_range, out_range, no_data = [], [], []
    for name, s in bones.items():
        if "median_m" not in s:
            no_data.append(name)
            continue
        lo, hi = BONE_LENGTH_RANGE_M[name]
        (in_range if lo <= s["median_m"] <= hi else out_range).append(name)
    if len(in_range) < _MIN_IN_RANGE_BONES:
        detail = ", ".join(
            f"{n} {bones[n]['median_m']:.2f}m" for n in out_range if "median_m" in bones[n]
        )
        issues.append(
            f"骨长中位在人体合理区间的段数 {len(in_range)}/12 不足 "
            f"{_MIN_IN_RANGE_BONES}（离谱段: {detail or '无'}"
            f"{'; 全部掩蔽: ' + ', '.join(no_data) if no_data else ''}）"
        )
    # 单段离谱即判异常（超出合理区间 2 倍 = 尺度/坐标系统性错误，非噪声）
    for n in out_range:
        if "median_m" not in bones[n]:
            continue
        lo, hi = BONE_LENGTH_RANGE_M[n]
        med = bones[n]["median_m"]
        if med > 2 * hi or med < 0.5 * lo:
            issues.append(
                f"骨长中位离谱: {n} {med:.2f}m（合理区间 {lo:.2f}–{hi:.2f}m 的 2 倍之外）"
            )

    # ---- 链路判定 2/3、3/3：深度分布（排除掩蔽点）----
    zs = np.asarray([
        kp["z"] for f in frames for kp in (f.get("joints") or []) if _joint_ok(kp)
    ], dtype=np.float64)
    if zs.size:
        p5, p50, p95 = (float(np.percentile(zs, q)) for q in (5, 50, 95))
        ratio_pos = float((zs > 0).mean())
    else:
        p5 = p50 = p95 = 0.0
        ratio_pos = 0.0
        issues.append("无任何有效 3D 点（全部掩蔽）")
    depth = {"p5_m": p5, "p50_m": p50, "p95_m": p95, "z_positive_ratio": ratio_pos}
    if zs.size and ratio_pos < _DEPTH_POSITIVE_MIN:
        issues.append(f"Z>0 比例 {ratio_pos:.1%} 低于 {_DEPTH_POSITIVE_MIN:.0%}（相机后方点过多）")
    if zs.size and not (_DEPTH_MEDIAN_RANGE[0] <= p50 <= _DEPTH_MEDIAN_RANGE[1]):
        lo, hi = _DEPTH_MEDIAN_RANGE
        issues.append(f"深度中位数 {p50:.2f}m 超出合理区间 [{lo:.0f}, {hi:.0f}]m")

    # ---- 低置信帧占比 ----
    low = sum(1 for f in frames if f.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD)
    low_ratio = low / n_frames if n_frames else 0.0

    # ---- 两维结论 ----
    def _cv_ok(name: str, thr: float) -> bool:
        s = bones.get(name) or {}
        return (s.get("valid_ratio", 0.0) >= _MIN_VALID_RATIO
                and s.get("robust_cv", float("inf")) < thr)

    n_stable = sum(1 for name, _i, _j in BONES if _cv_ok(name, _GRADE_A_CV))
    if issues:
        verdict = "链路异常"
        grade = None
    else:
        if n_stable == len(BONES):
            grade = "A"
        elif all(_cv_ok(n, _GRADE_B_LONG_CV) for n in LONG_BONES):
            grade = "B"
        else:
            grade = "C"
        verdict = f"链路合理（精度 {grade}）"

    # ---- 中文报告行 ----
    source = pose3d.get("calibration_source", "calibrated")
    if source == SOURCE_NOMINAL:
        lines.append("标定来源：名义标定，仅供链路冒烟（不能用于精度结论）")
    else:
        baseline = pose3d.get("calibration_baseline_mm")
        suffix = f"，基线 {baseline:.1f}mm" if isinstance(baseline, (int, float)) else ""
        lines.append(f"标定来源：实测标定{suffix}")
    lines.append(f"帧数：{n_frames}（低置信帧 {low}，占比 {low_ratio:.1%}）")
    lines.append(f"深度 Z（米，有效点）：p5 {p5:.2f} / 中位 {p50:.2f} / p95 {p95:.2f}，"
                 f"Z>0 比例 {ratio_pos:.1%}")
    lines.append("骨长（中位 m / 鲁棒CV = MAD÷中位 / 有效帧占比；"
                 "† = 中位超出人体合理区间）：")
    for name, _i, _j in BONES:
        s = bones.get(name) or {}
        if "median_m" in s:
            lo, hi = BONE_LENGTH_RANGE_M[name]
            flag = " †" if not (lo <= s["median_m"] <= hi) else ""
            lines.append(
                f"  {name}: {s['median_m']:.3f} m / {s['robust_cv']:.1%}"
                f" / {s['valid_ratio']:.0%}（{s['frames']} 帧）{flag}"
            )
        else:
            lines.append(f"  {name}: 无有效帧（全部掩蔽）")
    if sparse:
        lines.append(f"掩蔽过多（有效帧占比 <{_MIN_VALID_RATIO:.0%}，"
                     f"不参与评级）: {', '.join(sparse)}")
    lines.append(f"结论：{verdict}")
    if grade == "C":
        lines.append(f"改进建议：{GRADE_C_ADVICE}")
    if issues:
        lines.append("异常项：")
        lines.extend(f"  - {t}" for t in issues)

    return {
        "bones": bones,
        "depth": depth,
        "stable_bones": n_stable,
        "grade": grade,
        "low_confidence_frames": low,
        "low_confidence_ratio": low_ratio,
        "verdict": verdict,
        "issues": issues,
        "warnings": warnings,
        "lines": lines,
    }
