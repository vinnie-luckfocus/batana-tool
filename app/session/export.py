"""素材导出（PRD F10）：按 session-schema 契约产出素材目录 + 内置校验 + 可选全量校验。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import app
from app.capture.clip_writer import ClipWriter
from app.capture.ring_buffer import BufferItem
from app.detect.state_machine import Clip
from app.pose.io import write_pose2d
from app.pose.model import KEYPOINT_NAMES, PoseFrame
from app.session.ulid import new_session_id

SCHEMA_VERSION = "1.0"
PIPELINES = {"standard-vision", "standard-imu", "pro-fusion", "pro-stereo", "max"}
_SESSION_ID_RE = re.compile(r"^sess_[0-9A-HJKMNP-TV-Z]{26}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# batana-core 仓默认路径（存在时可调用其全量校验器）
DEFAULT_CORE_REPO = Path("/Users/vinniechow/Projects/private/batana-core")

# 未配置时的常见探测位置
_CORE_REPO_CANDIDATES = [
    DEFAULT_CORE_REPO,
    Path.home() / "Projects/private/batana-core",
    Path.home() / "Projects/batana-core",
]


def resolve_core_repo(configured: str = "") -> Path | None:
    """解析 batana-core 仓路径：settings 配置优先，否则探测常见位置；不可用返回 None。"""
    candidates = [Path(configured)] if configured else []
    candidates += _CORE_REPO_CANDIDATES
    for c in candidates:
        if core_validator_available(c):
            return c
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_session_json(
    session_id: str,
    clip_dir: Path,
    left_video: Path,
    frame_count: int,
    fps: float,
    resolution: tuple[int, int],
    handedness: str = "right",
    pipeline: str = "pro-stereo",
    device: dict[str, str] | None = None,
    subject: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    pose_model: str | None = None,
    pose_frames: list[PoseFrame] | None = None,
    video_sha256: bool = True,
) -> dict[str, Any]:
    """组装 session.json 数据（字段严格对齐 session-schema v1.0）。"""
    duration_ms = max(1, int(round(frame_count / fps * 1000)))
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "session_id": session_id,
            "created_at": _utc_now_iso(),
            "duration_ms": duration_ms,
            "device": device
            or {
                "model": "OV9281 双目模组（2560x800 SBS UVC）",
                "os": f"macOS {platform.mac_ver()[0]}",
            },
            "app": {"name": "batana-tool", "version": app.__version__},
            "pipeline": pipeline,
            "handedness": handedness,
        },
        "video": {
            "uri": left_video.resolve().as_uri(),
            "fps": fps,
            "resolution": [resolution[0], resolution[1]],
            "duration_ms": duration_ms,
            "view": "side",
        },
    }
    if video_sha256:
        data["video"]["sha256"] = _sha256(left_video)
    if subject:
        data["meta"]["subject"] = subject
    if calibration:
        data["calibration"] = calibration
    if pose_model and pose_frames is not None:
        data["pose2d"] = {
            "model": pose_model,
            "frame_rate": fps,
            "frames": [f.to_dict() for f in pose_frames],
        }
    return data


def validate_session_builtin(data: dict[str, Any]) -> list[str]:
    """内置轻量必填校验：对齐 session-schema 契约必填项，返回错误列表（空 = 通过）。

    全量校验请用 validate_with_core（需本机存在 batana-core 仓）。
    """
    errors: list[str] = []

    def err(path: str, msg: str) -> None:
        errors.append(f"{path}: {msg}")

    if not isinstance(data, dict):
        return ["根节点: 应为对象"]
    sv = data.get("schema_version")
    if not isinstance(sv, str) or not sv.startswith("1."):
        err("schema_version", "必填，主版本 1.x")

    meta = data.get("meta")
    if not isinstance(meta, dict):
        err("meta", "必填，应为对象")
    else:
        sid = meta.get("session_id")
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            err("meta.session_id", "必填，格式 sess_ + 26 位 Crockford Base32")
        created = meta.get("created_at")
        if not isinstance(created, str) or not _ISO_UTC_RE.match(created):
            err("meta.created_at", "必填，ISO 8601 UTC（Z 后缀）")
        dur = meta.get("duration_ms")
        if not isinstance(dur, int) or isinstance(dur, bool) or dur <= 0:
            err("meta.duration_ms", "必填，正整数（毫秒）")
        for section, keys in (("device", ("model", "os")), ("app", ("name", "version"))):
            sec = meta.get(section)
            if not isinstance(sec, dict) or any(
                not isinstance(sec.get(k), str) or not sec[k] for k in keys
            ):
                err(f"meta.{section}", f"必填，需含非空字符串字段 {keys}")
        if meta.get("pipeline") not in PIPELINES:
            err("meta.pipeline", f"必填，取值 {sorted(PIPELINES)}")
        if meta.get("handedness") not in ("right", "left"):
            err("meta.handedness", "必填，right / left")

    if "video" in data:
        video = data["video"]
        if not isinstance(video, dict):
            err("video", "应为对象")
        else:
            if not isinstance(video.get("uri"), str) or not video["uri"]:
                err("video.uri", "必填，非空字符串")
            if not isinstance(video.get("fps"), (int, float)) or video["fps"] <= 0:
                err("video.fps", "必填，正数")
            res = video.get("resolution")
            if not (
                isinstance(res, list)
                and len(res) == 2
                and all(isinstance(x, int) and x > 0 for x in res)
            ):
                err("video.resolution", "必填，[宽, 高] 正整数")

    if "pose2d" in data:
        pose = data["pose2d"]
        if not isinstance(pose, dict):
            err("pose2d", "应为对象")
        else:
            if not isinstance(pose.get("model"), str) or not pose["model"]:
                err("pose2d.model", "必填，非空字符串")
            if not isinstance(pose.get("frame_rate"), (int, float)) or pose["frame_rate"] <= 0:
                err("pose2d.frame_rate", "必填，正数")
            frames = pose.get("frames")
            if not isinstance(frames, list):
                err("pose2d.frames", "必填，应为数组")
            else:
                for i, f in enumerate(frames):
                    kps = f.get("keypoints") if isinstance(f, dict) else None
                    if not isinstance(kps, list) or len(kps) != 33:
                        err(f"pose2d.frames[{i}].keypoints", "应为 33 点数组")
                        continue
                    names = [k.get("name") if isinstance(k, dict) else None for k in kps]
                    if names != KEYPOINT_NAMES:
                        err(f"pose2d.frames[{i}].keypoints", "关键点名称或顺序与契约 33 点枚举不符")
                        continue
                    for j, k in enumerate(kps):
                        for field in ("x", "y", "visibility"):
                            v = k.get(field)
                            if not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
                                err(f"pose2d.frames[{i}].keypoints[{j}].{field}", "应在 [0,1]")
    return errors


def core_validator_available(core_repo: str | Path = DEFAULT_CORE_REPO) -> bool:
    """本机是否存在可调用的 batana-core 全量校验器。"""
    return (Path(core_repo) / "tools" / "validate_session.py").is_file()


def validate_with_core(
    session_json: str | Path,
    core_repo: str | Path = DEFAULT_CORE_REPO,
    require_metrics: bool = False,
) -> list[str] | None:
    """调用 batana-core tools/validate_session.py 做契约全量校验。

    返回错误列表（空 = 通过）；batana-core 仓不存在时返回 None。
    """
    core = Path(core_repo)
    if not core_validator_available(core):
        return None
    cmd = [sys.executable, "-m", "tools.validate_session", str(session_json)]
    if require_metrics:
        cmd.append("--require-metrics")
    proc = subprocess.run(cmd, cwd=core, capture_output=True, text=True, timeout=60)
    if proc.returncode == 0:
        return []
    lines = [
        line.strip()[2:] if line.strip().startswith("- ") else line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("- ")
    ]
    return lines or [proc.stdout.strip() or f"validate_session 退出码 {proc.returncode}"]


def export_session(
    clip: Clip,
    out_root: str | Path,
    pose_frames: list[PoseFrame] | None = None,
    pose_model: str | None = None,
    handedness: str = "right",
    pipeline: str = "pro-stereo",
    device: dict[str, str] | None = None,
    subject: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    capture_extra: dict[str, Any] | None = None,
    writer: ClipWriter | None = None,
    session_id: str | None = None,
    frames: Iterable[BufferItem] | None = None,
) -> Path:
    """把一段素材导出为契约目录，返回素材目录路径。

    产出（sessions/<session_id>/）：
        session.json / left.mkv / right.mkv / timestamps.csv / capture_meta.json
        （提供 pose_frames 时另有 pose2d.json）

    导出前执行内置必填校验，失败抛 ValueError。
    """
    sid = session_id or new_session_id()
    out_dir = Path(out_root) / "sessions" / sid
    items = list(frames) if frames is not None else clip.frames()
    if not items:
        raise ValueError("片段帧序列为空（环形缓冲可能容量不足，旧帧已被覆盖）")

    fps = writer.fps if writer else 120.0
    clip_writer = writer or ClipWriter(fps=fps)
    paths = clip_writer.write_clip(items, out_dir, meta=dict(capture_extra or {}))
    if pose_frames is not None and pose_model:
        write_pose2d(out_dir / "pose2d.json", pose_model, fps, pose_frames)

    h, w = items[0][2].shape[:2]
    data = build_session_json(
        session_id=sid,
        clip_dir=out_dir,
        left_video=paths.left_video,
        frame_count=paths.frame_count,
        fps=fps,
        resolution=(w, h),
        handedness=handedness,
        pipeline=pipeline,
        device=device,
        subject=subject,
        calibration=calibration,
        pose_model=pose_model,
        pose_frames=pose_frames,
    )
    errors = validate_session_builtin(data)
    if errors:
        raise ValueError("导出未通过内置契约校验:\n" + "\n".join(f"  - {e}" for e in errors))
    (out_dir / "session.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir
