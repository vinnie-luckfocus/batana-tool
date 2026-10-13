"""标定结果持久化：~/.batana-tool/calibration.json 的读写与状态摘要。

report.source 区分标定来源："nominal-spec" = 名义标定（规格估算），
其余（core calibrate_stereo 输出）= 实测标定。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CALIBRATION_PATH = Path.home() / ".batana-tool" / "calibration.json"

SOURCE_NOMINAL = "nominal-spec"


def save_calibration(data: dict, path: str | Path | None = None) -> Path:
    """保存标定结果（原子写入：临时文件 + os.replace），返回落盘路径。"""
    p = Path(path) if path else DEFAULT_CALIBRATION_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_calibration(path: str | Path | None = None) -> dict[str, Any] | None:
    """读取已存标定；文件不存在或解析失败返回 None。"""
    p = Path(path) if path else DEFAULT_CALIBRATION_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) and "stereo" in data else None


def calibration_status(path: str | Path | None = None) -> str:
    """标定状态一行摘要：无标定 / 名义标定 / 实测标定（日期 + RMS + 基线）。"""
    p = Path(path) if path else DEFAULT_CALIBRATION_PATH
    data = load_calibration(p)
    if data is None:
        return "无标定"
    report = data.get("report") or {}
    source = report.get("source", "calibrated")
    date = str(report.get("calibrated_at") or report.get("created_at") or "")
    if not date and p.is_file():
        date = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    baseline = (data.get("stereo") or {}).get("baseline_mm")
    if source == SOURCE_NOMINAL:
        text = f"名义标定（规格估算，基线 {baseline}mm，仅供冒烟）"
        return f"{text} · {date[:10]}" if date else text
    rms = (report.get("reprojection_error_px") or {}).get("stereo")
    parts = ["实测标定"]
    if date:
        parts.append(date[:10])
    if isinstance(rms, (int, float)):
        parts.append(f"立体 RMS {rms:.3f}px")
    if isinstance(baseline, (int, float)):
        parts.append(f"基线 {baseline:.1f}mm")
    return " · ".join(parts)
