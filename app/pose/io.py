"""pose2d.json 读写：结构对齐 session-schema 契约 pose2d 字段。

manual / auto 为契约允许的新增可选字段（只增不改），全量校验器会忽略未知字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pose.model import PoseFrame


def pose2d_to_dict(model: str, frame_rate: float, frames: list[PoseFrame]) -> dict:
    return {
        "model": model,
        "frame_rate": frame_rate,
        "frames": [f.to_dict() for f in frames],
    }


def write_pose2d(path: str | Path, model: str, frame_rate: float, frames: list[PoseFrame]) -> Path:
    p = Path(path)
    p.write_text(
        json.dumps(pose2d_to_dict(model, frame_rate, frames), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def read_pose2d(path: str | Path) -> tuple[str, float, list[PoseFrame]]:
    """读取 pose2d.json，返回 (model, frame_rate, frames)。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (
        data["model"],
        float(data["frame_rate"]),
        [PoseFrame.from_dict(f) for f in data.get("frames", [])],
    )


def merge_pose_frames(
    existing: list[PoseFrame],
    new: list[PoseFrame],
) -> list[PoseFrame]:
    """按 frame_index 合并两段骨架：new 覆盖同帧号的旧数据，其余保留。

    用于范围跑骨架（仅 trim 段）：范围外已有骨架数据保留，范围内用新推理结果；
    返回按 frame_index 排序的列表（可能稀疏——范围外无数据的帧仍无骨架）。
    """
    by_index = {f.frame_index: f for f in existing}
    for f in new:
        by_index[f.frame_index] = f
    return [by_index[k] for k in sorted(by_index)]
