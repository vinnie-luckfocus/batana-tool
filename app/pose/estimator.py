"""姿态估计器抽象：PoseEstimator protocol + Stub / MediaPipe 实现。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from app.pose.model import KEYPOINT_NAMES, Keypoint, PoseFrame


@runtime_checkable
class PoseEstimator(Protocol):
    """2D 姿态估计器：逐帧推理，输出 33 点 PoseFrame。"""

    model_name: str

    def estimate(self, frame_gray: np.ndarray, frame_index: int, timestamp_ms: float) -> PoseFrame:
        ...

    def close(self) -> None:
        ...


class StubPoseEstimator:
    """测试用确定性估计器：关键点按帧序号做正弦摆动，输出可复现。"""

    model_name = "batana-pose-stub-v0.0"

    def estimate(self, frame_gray: np.ndarray, frame_index: int, timestamp_ms: float) -> PoseFrame:
        t = frame_index / 30.0
        kps = []
        for i, name in enumerate(KEYPOINT_NAMES):
            x = 0.5 + 0.3 * math.sin(t + i * 0.19)
            y = 0.5 + 0.3 * math.cos(t * 0.7 + i * 0.23)
            kps.append(Keypoint(name=name, x=round(x, 6), y=round(y, 6), visibility=0.9))
        return PoseFrame(
            frame_index=frame_index,
            timestamp_ms=timestamp_ms,
            confidence=0.9,
            keypoints=kps,
        )

    def close(self) -> None:
        pass


class MediaPipePoseEstimator:
    """MediaPipe Tasks PoseLandmarker 实现（33 点 BlazePose 拓扑）。

    需要 pose landmarker 模型文件（.task），从 Google 官方地址下载：
    https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task
    mediapipe 不可导入或模型文件缺失时构造抛错，调用方应降级到 Stub 或跳过骨架。
    """

    model_name = "batana-pose-v0.1"

    @staticmethod
    def is_available() -> bool:
        try:
            from mediapipe.tasks.python import vision  # noqa: F401
        except Exception:
            return False
        return True

    def __init__(self, model_path: str | Path) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise FileNotFoundError(
                f"MediaPipe 模型文件不存在: {model_path}（下载地址见 README）"
            )
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def estimate(self, frame_gray: np.ndarray, frame_index: int, timestamp_ms: float) -> PoseFrame:
        import mediapipe as mp

        rgb = np.stack([frame_gray] * 3, axis=-1) if frame_gray.ndim == 2 else frame_gray
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._landmarker.detect_for_video(mp_image, int(timestamp_ms))
        kps: list[Keypoint] = []
        if result.pose_landmarks:
            landmarks = result.pose_landmarks[0]
            for i, name in enumerate(KEYPOINT_NAMES):
                lm = landmarks[i]
                vis = float(getattr(lm, "visibility", 1.0) or 0.0)
                kps.append(
                    Keypoint(
                        name=name,
                        x=min(max(float(lm.x), 0.0), 1.0),
                        y=min(max(float(lm.y), 0.0), 1.0),
                        visibility=min(max(vis, 0.0), 1.0),
                    )
                )
            confidence = sum(k.visibility for k in kps) / len(kps)
        else:
            # 未检出：33 点 visibility=0（契约：缺失点 visibility=0）
            kps = [Keypoint(name=n, x=0.0, y=0.0, visibility=0.0) for n in KEYPOINT_NAMES]
            confidence = 0.0
        return PoseFrame(
            frame_index=frame_index,
            timestamp_ms=timestamp_ms,
            confidence=round(confidence, 6),
            keypoints=kps,
        )

    def close(self) -> None:
        self._landmarker.close()
