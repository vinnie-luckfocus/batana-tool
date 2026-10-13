"""调用 batana-core 的 ChArUco 双目标定 CLI（subprocess，模式同 validate_with_core）。

sys.executable 保证用本仓虚拟环境解释器；cwd=core_repo 使 core 的
tools.calibration.calibrate_stereo 模块可解析。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_CALIB_TIMEOUT_S = 300


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_core_calibration(
    frames_dir: str | Path,
    core_repo: str | Path,
    out_json: str | Path,
    square_mm: float = 100.0,
    squares_x: int = 11,
    squares_y: int = 8,
) -> dict:
    """跑 core 标定，成功返回标定 dict；失败把 stdout/stderr 浓缩进 ValueError。"""
    core = Path(core_repo)
    if not (core / "tools" / "calibration" / "calibrate_stereo.py").is_file():
        raise ValueError(f"batana-core 仓不可用（缺 calibrate_stereo）: {core}")
    cmd = [
        sys.executable, "-m", "tools.calibration.calibrate_stereo", str(frames_dir),
        "--out-json", str(out_json),
        "--out-yaml", str(Path(out_json).with_suffix(".yaml")),  # 默认会写 cwd，显式指到同目录
        "--square-mm", str(square_mm),
        "--squares-x", str(squares_x),
        "--squares-y", str(squares_y),
    ]
    try:
        proc = subprocess.run(cmd, cwd=core, capture_output=True, text=True,
                              timeout=_CALIB_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ValueError(f"标定超时（>{_CALIB_TIMEOUT_S}s）: {frames_dir}") from None
    if proc.returncode != 0 or not Path(out_json).is_file():
        detail = "\n".join(
            line for line in (proc.stdout, proc.stderr) if line and line.strip()
        )
        # 浓缩：只留最后若干行，避免弹窗/异常被整段日志淹没
        tail = "\n".join(detail.splitlines()[-12:])
        raise ValueError(f"core 标定失败（退出码 {proc.returncode}）:\n{tail}")
    data = json.loads(Path(out_json).read_text(encoding="utf-8"))
    report = data.setdefault("report", {})
    report.setdefault("source", "calibrated")
    report["calibrated_at"] = _utc_now_iso()
    return data
