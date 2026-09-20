"""合成双目测试视频生成器：无头端到端测试的数据源。

场景（每个循环）：无人 → 人形剪影走入 ROI 就位 → 快速挥棒（运动能量尖峰）→ 静止。
输出 2560x800（默认）SBS 灰度视频，左右目加水平视差（默认 8px），FFV1/MKV 优先。

用法：
    python -m samples.gen_synth out.mkv --fps 30 --cycles 2
"""

from __future__ import annotations

import argparse
import math
import warnings
from pathlib import Path

import cv2
import numpy as np

BACKGROUND = 30
PERSON = 190


def _draw_person(
    img: np.ndarray,
    cx: int,
    arm_deg: float = -20.0,
    value: int = PERSON,
) -> None:
    """在单目灰度图上画人形剪影（头/躯干/双腿/手臂+棒）。

    cx: 人中心 x；arm_deg: 手臂角度（0=水平向前，正值向下，负值抬起）。
    """
    h = img.shape[0]
    ground_y = int(h * 0.95)
    person_h = int(h * 0.70)
    hip_y = ground_y - int(person_h * 0.45)
    shoulder_y = ground_y - int(person_h * 0.85)
    body_w = max(4, int(h * 0.14))
    leg_w = max(2, int(h * 0.05))
    head_r = max(3, int(h * 0.07))
    thick = max(2, int(h * 0.035))

    # 双腿
    cv2.rectangle(img, (cx - body_w // 2, hip_y), (cx - body_w // 2 + leg_w, ground_y), value, -1)
    cv2.rectangle(img, (cx + body_w // 2 - leg_w, hip_y), (cx + body_w // 2, ground_y), value, -1)
    # 躯干
    cv2.rectangle(img, (cx - body_w // 2, shoulder_y), (cx + body_w // 2, hip_y), value, -1)
    # 头
    cv2.circle(img, (cx, shoulder_y - head_r - 2), head_r, value, -1)
    # 手臂 + 棒（从肩部出发两段线）
    sx, sy = cx, shoulder_y + thick
    arm_len = int(h * 0.30)
    rad = math.radians(arm_deg)
    ex = int(sx + arm_len * math.cos(rad))
    ey = int(sy + arm_len * math.sin(rad))
    cv2.line(img, (sx, sy), (ex, ey), value, thick)
    bat_len = int(h * 0.22)
    bx = int(ex + bat_len * math.cos(rad))
    by = int(ey + bat_len * math.sin(rad))
    cv2.line(img, (ex, ey), (bx, by), value, max(1, thick - 1))


def _shift_horizontal(frame: np.ndarray, dx: int, fill: int = BACKGROUND) -> np.ndarray:
    """水平平移（双目视差），空出的边缘用背景值填充。"""
    out = np.full_like(frame, fill)
    if dx > 0:
        out[:, dx:] = frame[:, :-dx]
    elif dx < 0:
        out[:, :dx] = frame[:, -dx:]
    else:
        out[:] = frame
    return out


def _render_left(
    w: int,
    h: int,
    person_cx: int | None,
    arm_deg: float,
) -> np.ndarray:
    img = np.full((h, w), BACKGROUND, np.uint8)
    if person_cx is not None:
        # 人形完全静止：就位检测靠 MOG2 慢学习率（learning_rate≈0.002）保持前景，
        # 静止期运动能量只剩噪声底，挥棒"回落结束"才能可靠触发
        _draw_person(img, person_cx, arm_deg=arm_deg)
    return img


def generate(
    out_path: str | Path,
    fps: float = 30.0,
    width: int = 2560,
    height: int = 800,
    cycles: int = 2,
    disparity_px: int = 8,
    empty_s: float = 2.0,
    walkin_s: float = 2.0,
    swing_s: float = 0.5,
    still_s: float = 1.5,
    seed: int = 42,
) -> Path:
    """生成合成 SBS 双目视频，返回输出路径。

    每个循环：[无人 empty_s] → [走入+就位 walkin_s] → [挥棒 swing_s] → [静止 still_s]。
    人形就位位置：单目画面中心（cx = w/2）；挥棒为手臂+棒快速下扫并伴随躯干前倾。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    eye_w = width // 2
    stance_cx = eye_w // 2
    rng = np.random.default_rng(seed)

    writer: cv2.VideoWriter | None = None
    actual = out
    for codec, ext in (("FFV1", ".mkv"), ("mp4v", ".mp4")):
        actual = out if out.suffix == ext else out.with_suffix(ext)
        w4 = cv2.VideoWriter(str(actual), cv2.VideoWriter_fourcc(*codec), fps, (width, height), isColor=False)
        if w4.isOpened():
            writer = w4
            if codec != "FFV1":
                warnings.warn(f"FFV1 不可用，合成视频回退到 {codec}", RuntimeWarning, stacklevel=2)
            break
        w4.release()
    if writer is None:
        raise RuntimeError("无可用视频编码器")

    try:
        for _ in range(cycles):
            # 1) 无人
            for _i in range(int(empty_s * fps)):
                left = _render_left(eye_w, height, None, 0.0)
                _write(writer, left, disparity_px, rng)
            # 2) 走入 + 就位（前 60% 走入，后 40% 站立）
            n = int(walkin_s * fps)
            for i in range(n):
                frac = min(1.0, i / max(1, int(n * 0.6)))
                cx = int(eye_w * 1.05 + (stance_cx - eye_w * 1.05) * frac)
                left = _render_left(eye_w, height, cx, arm_deg=-20.0)
                _write(writer, left, disparity_px, rng)
            # 3) 挥棒：手臂 -100° → +70° 快速下扫 + 躯干前倾（运动能量尖峰）
            n = max(3, int(swing_s * fps))
            for i in range(n):
                frac = i / (n - 1)
                arm_deg = -100.0 + 170.0 * frac
                lunge = int(eye_w * 0.06 * frac)  # 躯干前倾位移
                left = _render_left(eye_w, height, stance_cx + lunge, arm_deg=arm_deg)
                _write(writer, left, disparity_px, rng)
            # 4) 静止
            for _i in range(int(still_s * fps)):
                left = _render_left(eye_w, height, stance_cx, arm_deg=70.0)
                _write(writer, left, disparity_px, rng)
    finally:
        writer.release()
    return actual


def _write(writer: cv2.VideoWriter, left: np.ndarray, disparity_px: int, rng: np.random.Generator) -> None:
    right = _shift_horizontal(left, disparity_px)
    sbs = np.concatenate([left, right], axis=1)
    noise = rng.normal(0.0, 0.8, sbs.shape)
    sbs = np.clip(sbs.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    writer.write(sbs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合成双目测试视频生成器")
    parser.add_argument("out", help="输出路径（.mkv；FFV1 不可用时回退 .mp4）")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--disparity", type=int, default=8, help="左右目水平视差（像素）")
    args = parser.parse_args(argv)
    path = generate(
        args.out, fps=args.fps, width=args.width, height=args.height,
        cycles=args.cycles, disparity_px=args.disparity,
    )
    print(f"已生成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
