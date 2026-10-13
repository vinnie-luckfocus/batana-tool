"""名义标定：无标定板时按模组规格估算的内参/外参（仅供链路冒烟）。

HBVCAM-W2237-2 V11 双目模组规格（实测报告确认）：
- 基线固定 120mm；
- OV9281 像元 3µm，镜头焦距 f≈3.6mm → 1280 宽单目 f_px = 3.6mm/3µm ≈ 1200；
- 其他分辨率按宽度线性缩放：f_px = 1200 × (width / 1280)；
- 主点取画面中心 (w/2-0.5, h/2-0.5)，畸变全 0，旋转单位阵。

这是估算值，不代表真实镜头个体偏差，不能用于任何精度结论。
"""

from __future__ import annotations

from datetime import datetime, timezone

# 模组规格常量（HBVCAM-W2237-2 V11）
NOMINAL_BASELINE_MM = 120.0
_REFERENCE_WIDTH = 1280
_REFERENCE_F_PX = 1200.0  # f 3.6mm / 像元 3µm，1280 宽单目


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def nominal_calibration(
    width: int,
    height: int,
    baseline_mm: float = NOMINAL_BASELINE_MM,
) -> dict:
    """生成名义标定 dict，结构与 core calibrate_stereo 输出完全一致。

    report.source = "nominal-spec" 标记估算来源，消费方据此区分实测标定。
    """
    f_px = _REFERENCE_F_PX * (width / _REFERENCE_WIDTH)
    cx, cy = width / 2 - 0.5, height / 2 - 0.5
    intrinsic = [[f_px, 0.0, cx], [0.0, f_px, cy], [0.0, 0.0, 1.0]]
    eye = {
        "resolution": [int(width), int(height)],
        "intrinsic": intrinsic,
        "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
    }
    return {
        "format_version": "1.0",
        "stereo": {
            "baseline_mm": float(baseline_mm),
            "left": dict(eye),
            "right": dict(eye),
            "extrinsic": {
                "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "translation_mm": [float(baseline_mm), 0.0, 0.0],
            },
        },
        "report": {
            "source": "nominal-spec",
            "created_at": _utc_now_iso(),
            "basis": (
                "HBVCAM-W2237-2 V11 模组规格估算：基线 120mm（实测报告确认）；"
                "OV9281 像元 3µm、镜头 f≈3.6mm → 1280 宽单目 f_px≈1200，"
                "按分辨率线性缩放；主点取画面中心，畸变全 0。"
                "这是估算值，仅供链路冒烟，不能用于精度结论。"
            ),
        },
    }
