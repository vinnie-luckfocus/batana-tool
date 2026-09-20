"""无头环境体检 CLI（演示/调试）：

    python -m app.envcheck --source <视频文件> --duration 10 [--roi x,y,w,h] [--report-root DIR]

退出码：0 可以采集 / 1 建议整改 / 2 不可采集（便于脚本串联）。
"""

from __future__ import annotations

import argparse
import sys

from app.capture import FileSource
from app.envcheck import EnvironmentChecker, EnvironmentReport
from app.envcheck.models import OVERALL_FAIL, OVERALL_OK, OVERALL_WARN, STATUS_LABELS

_EXIT_CODES = {OVERALL_OK: 0, OVERALL_WARN: 1, OVERALL_FAIL: 2}


def _parse_roi(text: str | None) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI 格式: x,y,w,h")
    return (parts[0], parts[1], parts[2], parts[3])


def print_report(report: EnvironmentReport) -> None:
    """终端表格打印逐项结果与 overall 结论。"""
    print(f"=== 环境体检报告 ({report.created_at}) ===")
    print(f"采样 {report.duration_s:.1f}s，检查耗时 {report.elapsed_s:.1f}s")
    print(f"{'状态':<6}{'检查项':<10}{'实测值':<28}建议")
    print("-" * 80)
    for r in report.results:
        print(f"{STATUS_LABELS[r.status]:<6}{r.name:<10}{r.measured:<28}{r.suggestion}")
    print("-" * 80)
    print(f">>> 结论: {report.overall}")
    if report.report_path:
        print(f">>> 报告已保存: {report.report_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.envcheck", description="环境合规主动判定（无头）")
    parser.add_argument("--source", required=True, help="视频文件路径（FileSource 回放）")
    parser.add_argument("--duration", type=float, default=10.0, help="采样时长（秒）")
    parser.add_argument("--roi", default=None, help="左目 ROI: x,y,w,h（默认中央区域）")
    parser.add_argument("--report-root", default=None, help="报告落盘根目录（env_reports/ 下）")
    args = parser.parse_args(argv)

    source = FileSource(args.source)
    checker = EnvironmentChecker(source, roi=_parse_roi(args.roi), report_root=args.report_root)
    report = checker.run(duration_s=args.duration)
    source.close()
    print_report(report)
    return _EXIT_CODES.get(report.overall, 0)


if __name__ == "__main__":
    sys.exit(main())
