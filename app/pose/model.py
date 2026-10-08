"""姿态数据模型：MediaPipe BlazePose 33 关键点 + 手动修正标记。

点序与命名严格对齐 batana-core session-schema 契约 pose2d 字段约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 固定 33 点（MediaPipe BlazePose 拓扑，顺序与契约一致，不可改动）
KEYPOINT_NAMES = [
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky",
    "left_index", "right_index", "left_thumb", "right_thumb",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
]
assert len(KEYPOINT_NAMES) == 33


@dataclass
class Keypoint:
    """单个关键点：归一化坐标 [0,1]（原点左上）+ 可见性。

    manual=True 表示经人工拖动修正；auto 保留修正前的自动原值。
    """

    name: str
    x: float
    y: float
    visibility: float
    manual: bool = False
    auto: dict[str, float] | None = None

    def to_dict(self) -> dict:
        d: dict = {"name": self.name, "x": self.x, "y": self.y, "visibility": self.visibility}
        if self.manual:
            d["manual"] = True
            if self.auto is not None:
                d["auto"] = self.auto
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Keypoint":
        return cls(
            name=d["name"],
            x=float(d["x"]),
            y=float(d["y"]),
            visibility=float(d["visibility"]),
            manual=bool(d.get("manual", False)),
            auto=d.get("auto"),
        )


@dataclass
class PoseFrame:
    """单帧姿态：33 点 + 整帧置信度。"""

    frame_index: int
    timestamp_ms: float
    confidence: float
    keypoints: list[Keypoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.keypoints) != 33:
            raise ValueError(f"PoseFrame 必须含 33 个关键点，实际 {len(self.keypoints)}")

    def correct(self, keypoint: int | str, x: float, y: float, visibility: float | None = None) -> None:
        """手动修正关键点：标记 manual 并保留自动原值（重复修正保留最早原值）。"""
        idx = KEYPOINT_NAMES.index(keypoint) if isinstance(keypoint, str) else keypoint
        kp = self.keypoints[idx]
        if not kp.manual:
            kp.auto = {"x": kp.x, "y": kp.y, "visibility": kp.visibility}
        kp.x = float(x)
        kp.y = float(y)
        kp.visibility = float(visibility) if visibility is not None else 1.0
        kp.manual = True

    def to_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "timestamp_ms": self.timestamp_ms,
            "confidence": self.confidence,
            "keypoints": [kp.to_dict() for kp in self.keypoints],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PoseFrame":
        return cls(
            frame_index=int(d["frame_index"]),
            timestamp_ms=float(d["timestamp_ms"]),
            confidence=float(d["confidence"]),
            keypoints=[Keypoint.from_dict(k) for k in d["keypoints"]],
        )
