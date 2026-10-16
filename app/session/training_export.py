"""训练集导出：片段目录（视频 + 双骨架）→ 训练包目录（PNG 帧 + annotations + meta）。

供 batana-core 后续微调姿态模型直接消费。坐标一律像素值
（由 pose2d 归一化坐标 × per_eye_resolution 还原），保留人工修正值与 manual 标记。
无 Qt 依赖，可无头单测。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cv2

from app.pose.io import read_pose2d

_VIDEO_EXTS = (".mkv", ".mp4")


def _find_video(clip: Path, eye: str) -> Path:
    for ext in _VIDEO_EXTS:
        p = clip / f"{eye}{ext}"
        if p.is_file():
            return p
    raise ValueError(f"片段缺少 {eye} 目视频: {clip}")


def _read_pose_by_index(clip: Path, eye: str) -> dict[int, list[dict]]:
    """按目读骨架（左目 pose2d.json / 右目 pose2d_right.json），返回
    {frame_index: 33 点 dict 列表}；文件不存在返回空 dict（该目全部写 null）。"""
    path = clip / ("pose2d.json" if eye == "left" else "pose2d_right.json")
    if not path.is_file():
        return {}
    _model, _fps, frames = read_pose2d(path)
    out: dict[int, list[dict]] = {}
    for f in frames:
        out[f.frame_index] = [kp.to_dict() for kp in f.keypoints]
    return out


def _to_pixel_keypoints(kps: list[dict], width: int, height: int) -> list[dict]:
    """归一化关键点 → 像素坐标，保留 manual 标记（有修正时保留 auto 原值）。"""
    out = []
    for kp in kps:
        item = {
            "name": kp["name"],
            "x": float(kp["x"]) * width,
            "y": float(kp["y"]) * height,
            "visibility": float(kp["visibility"]),
        }
        if kp.get("manual"):
            item["manual"] = True
            if kp.get("auto") is not None:
                item["auto"] = kp["auto"]
        out.append(item)
    return out


def export_training_pack(
    clip_dir: str | Path,
    out_root: str | Path,
    trim: tuple[int, int] | None = None,
) -> Path:
    """导出训练包到 out_root/<session_id>/，返回该目录路径。

    产出：
    - images/left_000123.png / right_000123.png（trim 段内帧，帧号 6 位）；
    - annotations.json：逐帧双目 33 点像素坐标（某目该帧无骨架写 null）；
    - meta.json：来源片段、导出时间、帧数与用途说明。
    trim 缺省为整段；闭区间。
    """
    clip = Path(clip_dir)
    meta_path = clip / "capture_meta.json"
    if not meta_path.is_file():
        raise ValueError(f"片段缺少 capture_meta.json: {clip}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    width, height = (int(v) for v in meta["per_eye_resolution"])
    fps = float(meta.get("fps", 120.0))

    # 先读视频帧（灰度），确定段内范围
    caps = {eye: cv2.VideoCapture(str(_find_video(clip, eye))) for eye in ("left", "right")}
    for eye, cap in caps.items():
        if not cap.isOpened():
            raise ValueError(f"无法打开 {eye} 目视频: {clip}")
    try:
        frames: dict[str, list] = {"left": [], "right": []}
        for eye, cap in caps.items():
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frames[eye].append(frame)
    finally:
        for cap in caps.values():
            cap.release()
    total = min(len(frames["left"]), len(frames["right"]))
    if total == 0:
        raise ValueError(f"视频无可用帧: {clip}")

    start, end = trim if trim is not None else (0, total - 1)
    start = max(0, int(start))
    end = min(total - 1, int(end))
    if start > end:
        raise ValueError(f"trim 区间非法: [{start}, {end}]（总帧数 {total}）")

    session_id = clip.name
    out = Path(out_root) / session_id
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)

    pose_by_eye = {eye: _read_pose_by_index(clip, eye) for eye in ("left", "right")}

    ann_frames = []
    for i in range(start, end + 1):
        for eye in ("left", "right"):
            if not cv2.imwrite(str(images / f"{eye}_{i:06d}.png"), frames[eye][i]):
                raise ValueError(f"写帧失败: {images / f'{eye}_{i:06d}.png'}")
        eyes = {}
        for eye in ("left", "right"):
            kps = pose_by_eye[eye].get(i)
            eyes[eye] = ({"keypoints": _to_pixel_keypoints(kps, width, height)}
                         if kps is not None else None)
        ann_frames.append({
            "frame_index": i,
            "timestamp_ms": i / fps * 1000.0,
            "eyes": eyes,
        })

    annotations = {
        "format_version": "1.0",
        "session_id": session_id,
        "fps": fps,
        "per_eye_resolution": [width, height],
        "trim": {"start_frame": start, "end_frame": end},
        "frames": ann_frames,
    }
    (out / "annotations.json").write_text(
        json.dumps(annotations, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    meta_out = {
        "source_clip": str(clip),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "frame_count": len(ann_frames),
        "note": "batana-tool 训练包：images/ 双目 PNG 帧 + annotations.json 逐帧 33 点像素坐标"
                "（含人工修正 manual 标记，某目无骨架为 null）；供 batana-core 微调姿态模型消费。",
    }
    (out / "meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
