"""环境检查数据模型：单项结果与整体报告（纯 dataclass + json，无 Qt 依赖）。

报告 JSON 序列化（to_dict/from_dict），落盘到存储根目录 env_reports/<时间戳>.json。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---- 状态枚举（字符串常量，JSON 友好） ----
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUSES = (STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIP)

STATUS_LABELS = {
    STATUS_PASS: "合格",
    STATUS_WARN: "警告",
    STATUS_FAIL: "不合格",
    STATUS_SKIP: "跳过",
}

# ---- overall 结论 ----
OVERALL_OK = "可以采集"
OVERALL_WARN = "建议整改"
OVERALL_FAIL = "不可采集"


@dataclass
class CheckResult:
    """单项环境检查结果。"""

    check_id: str       # 英文标识，如 "brightness"
    name: str           # 中文名称，如 "光照充足性"
    status: str         # pass / warn / fail / skip
    measured: str       # 实测值字符串，如 "ROI 均值 142"
    suggestion: str     # 中文整改建议（pass/skip 可为空）

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "status": self.status,
            "measured": self.measured,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        return cls(
            check_id=data["check_id"],
            name=data["name"],
            status=data["status"],
            measured=data.get("measured", ""),
            suggestion=data.get("suggestion", ""),
        )


@dataclass
class EnvironmentReport:
    """环境体检报告：逐项结果 + overall 结论 + 耗时。"""

    results: list[CheckResult] = field(default_factory=list)
    duration_s: float = 0.0     # 实际采样时长（秒）
    elapsed_s: float = 0.0      # 检查总耗时（秒）
    created_at: str = ""        # ISO 时间戳
    report_path: str = ""       # 落盘路径（save() 后回填）

    @property
    def overall(self) -> str:
        """有 fail → 不可采集；有 warn → 建议整改；其余 → 可以采集。"""
        statuses = {r.status for r in self.results}
        if STATUS_FAIL in statuses:
            return OVERALL_FAIL
        if STATUS_WARN in statuses:
            return OVERALL_WARN
        return OVERALL_OK

    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at,
            "duration_s": round(self.duration_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "overall": self.overall,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnvironmentReport":
        return cls(
            results=[CheckResult.from_dict(r) for r in data.get("results", [])],
            duration_s=float(data.get("duration_s", 0.0)),
            elapsed_s=float(data.get("elapsed_s", 0.0)),
            created_at=data.get("created_at", ""),
        )

    def save(self, storage_root: str | Path) -> Path:
        """落盘到 <storage_root>/env_reports/env_<时间戳>.json，返回路径。"""
        out_dir = Path(storage_root) / "env_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"env_{stamp}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.report_path = str(path)
        return path
