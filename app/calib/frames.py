"""标定帧抽取：从片段目录的 left/right 视频均匀抽帧，写成 core 标定的分图命名。

输出命名 left_XXXX.png / right_XXXX.png（calibrate_stereo 分图模式约定：
文件名含 left/right，按去前缀公共键配对）。
"""

from __future__ import annotations

from pathlib import Path

import cv2

# 编码回退兼容：FFV1 优先（.mkv），不可用时 ClipWriter 回退 .mp4
_VIDEO_NAMES = (".mkv", ".mp4")


def _find_video(clip_dir: Path, eye: str) -> Path:
    for ext in _VIDEO_NAMES:
        p = clip_dir / f"{eye}{ext}"
        if p.is_file():
            return p
    raise ValueError(f"片段目录缺少 {eye} 目视频（{[f'{eye}{e}' for e in _VIDEO_NAMES]}）: {clip_dir}")


def _read_all_frames(path: Path) -> list:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {path}")
    frames = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        cap.release()
    return frames


def extract_frame_pairs(
    clip_dir: str | Path,
    out_dir: str | Path,
    max_frames: int = 20,
) -> int:
    """均匀抽取左右目各 max_frames 帧写为 PNG 对，返回写出对数。

    视频打不开、帧数为 0 或左右目帧数不一致时抛 ValueError。
    """
    clip = Path(clip_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    left_path = _find_video(clip, "left")
    right_path = _find_video(clip, "right")
    left_frames = _read_all_frames(left_path)
    right_frames = _read_all_frames(right_path)
    if not left_frames:
        raise ValueError(f"视频无可用帧: {left_path}")
    if len(left_frames) != len(right_frames):
        raise ValueError(
            f"左右目帧数不一致: {len(left_frames)} vs {len(right_frames)}（{clip}）"
        )

    n = len(left_frames)
    step = max(1, n // max_frames)
    indices = list(range(0, n, step))[:max_frames]
    count = 0
    for i, idx in enumerate(indices):
        if not cv2.imwrite(str(out / f"left_{i:04d}.png"), left_frames[idx]):
            raise ValueError(f"写帧失败: {out / f'left_{i:04d}.png'}")
        if not cv2.imwrite(str(out / f"right_{i:04d}.png"), right_frames[idx]):
            raise ValueError(f"写帧失败: {out / f'right_{i:04d}.png'}")
        count += 1
    return count
