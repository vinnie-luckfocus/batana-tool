"""环境检查阈值默认值（PRD F11）：全部可调，设置页「环境检查」区暴露。

阈值依据拍摄环境硬约束：曝光 ≤100µs 要求击球区照度 ≥10,000 lux
（室内笼灯通常 300–500 lux）；照明无频闪（帧均值帧间波动 <2% 合格）；
相机条须水平；正侧面 2–3.5m。
"""

from __future__ import annotations

from dataclasses import dataclass

MAINS_FREQS_HZ = (100.0, 120.0)  # 市电频闪特征频率（50/60Hz 整流后倍频）


@dataclass
class EnvCheckSettings:
    """环境体检 8 项检查的阈值参数集合。"""

    # 1 光照充足性（ROI 灰度均值，0–255）
    brightness_fail: float = 60.0     # <此值 → fail
    brightness_warn: float = 90.0     # fail–warn 区间 → warn；warn–high → pass
    brightness_high: float = 200.0    # >此值且高光占比超阈 → warn（局部过曝）
    highlight_level: int = 250        # 高光判定灰度
    highlight_ratio: float = 0.05     # 高光像素占比阈值
    # 2 频闪（帧均值序列 (max-min)/mean 百分比）
    flicker_warn_pct: float = 2.0
    flicker_fail_pct: float = 5.0
    mains_tol_hz: float = 6.0         # FFT 主频落入 100/120Hz±tol 判定市电特征
    # 3 清晰度（ROI Laplacian 方差均值，注意：runner 在半分辨率帧上计算）
    sharpness_warn: float = 30.0
    # 4 背景干扰（ROI 外区域帧差能量均值）
    bg_motion_thresh: float = 4.0
    # 5 相机水平（主水平线倾角，度）
    level_warn_deg: float = 2.0
    level_fail_deg: float = 5.0
    level_min_lines: int = 3          # 少于此直线数 → skip
    # 6 构图覆盖
    framing_tight_margin: float = 0.05      # 头顶/脚底距 ROI 边缘 <5% → warn
    framing_min_height_ratio: float = 0.40  # 人形高度 <40% ROI 高度 → warn（距离过远）
    # 7 帧率/掉帧（与核心层口径一致：间隔 >1.5×标称帧间隔计为掉帧）
    drop_gap_factor: float = 1.5
    drop_warn_rate: float = 0.001     # 掉帧率 ≥0.1% → warn
    drop_fail_rate: float = 0.01      # 掉帧率 ≥1% → fail
    # 8 磁盘（独立于帧检查）
    planned_clips: int = 200          # 计划采集段数
    est_mb_per_clip: float = 500.0    # 单段估算大小（MB）
    disk_warn_factor: float = 2.0     # 余量 <2×需求 → warn；<需求 → fail

    @classmethod
    def from_app_settings(cls, s) -> "EnvCheckSettings":
        """从 AppSettings（app.ui.settings）的 env_ 前缀字段映射。"""
        return cls(
            brightness_fail=s.env_brightness_fail,
            brightness_warn=s.env_brightness_warn,
            flicker_warn_pct=s.env_flicker_warn_pct,
            flicker_fail_pct=s.env_flicker_fail_pct,
            sharpness_warn=s.env_sharpness_warn,
            level_warn_deg=s.env_level_warn_deg,
            level_fail_deg=s.env_level_fail_deg,
            planned_clips=s.env_planned_clips,
            est_mb_per_clip=s.env_est_mb_per_clip,
        )
