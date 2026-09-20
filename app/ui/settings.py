"""界面设置持久化：settings.json（ROI、采集模式、检测阈值、语音、存储根目录）。

纯 dataclass + json，无 Qt 依赖，可无头测试。
默认路径 ~/.batana-tool/settings.json；存储根目录默认 ~/batana-sessions。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SETTINGS_PATH = Path.home() / ".batana-tool" / "settings.json"
DEFAULT_STORAGE_ROOT = str(Path.home() / "batana-sessions")

# ROI = (x, y, w, h)，左目像素坐标；None 表示未框选
RoiTuple = tuple[int, int, int, int]


@dataclass
class AppSettings:
    """采集与界面参数（对应 PRD F2/F3/F4/F5 可调项）。"""

    # F2 打击区 ROI（左目像素坐标）
    roi: list[int] | None = None
    # F1 采集模式
    capture_width: int = 2560
    capture_height: int = 800
    capture_fps: float = 120.0
    pixel_format: str = "auto"  # auto / mono8 / yuy2 / mjpeg
    camera_index: int = 0
    # F3/F5 检测阈值
    presence_ratio: float = 0.02
    energy_trigger: float = 15.0
    energy_release: float = 8.0
    pre_roll_seconds: float = 1.0
    post_roll_seconds: float = 1.0
    countdown_seconds: float = 3.0
    buffer_seconds: float = 3.0
    # F4 语音
    voice_enabled: bool = True
    voice_rate: int = 200  # say -r（词/分钟）
    # F6 存储
    storage_root: str = DEFAULT_STORAGE_ROOT
    # F7 骨架（MediaPipe 模型文件路径；空 = 未配置，用 Stub）
    pose_model_path: str = ""
    # F10 导出校验：batana-core 仓路径（空 = 自动探测常见位置）
    core_repo_path: str = ""
    # F11 环境检查（阈值默认值见 app/envcheck/defaults.py，设置页可调）
    env_brightness_fail: float = 60.0
    env_brightness_warn: float = 90.0
    env_flicker_warn_pct: float = 2.0
    env_flicker_fail_pct: float = 5.0
    env_sharpness_warn: float = 30.0
    env_level_warn_deg: float = 2.0
    env_level_fail_deg: float = 5.0
    env_planned_clips: int = 200
    env_est_mb_per_clip: float = 500.0

    _path: Path = field(default=DEFAULT_SETTINGS_PATH, repr=False, compare=False)

    # ---- 持久化 ----

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppSettings":
        p = Path(path) if path else DEFAULT_SETTINGS_PATH
        settings = cls()
        settings._path = p
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            for key, value in data.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
        return settings

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else self._path
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("_path", None)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        self._path = p
        return p

    # ---- ROI 便捷访问 ----

    def roi_tuple(self) -> RoiTuple | None:
        if self.roi is None:
            return None
        return (int(self.roi[0]), int(self.roi[1]), int(self.roi[2]), int(self.roi[3]))

    def set_roi(self, roi: RoiTuple) -> None:
        self.roi = [int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])]
