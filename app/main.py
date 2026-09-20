"""batana-tool 入口。UI 页面在下一里程碑实现；当前提供命令行自检与合成素材生成。"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batana-tool", description="batana-core 素材采集与标注工具")
    parser.add_argument(
        "--gen-synth",
        metavar="OUT",
        help="生成合成双目测试视频到 OUT（等价 samples/gen_synth.py）",
    )
    parser.add_argument("--fps", type=float, default=30.0, help="合成视频帧率（默认 30）")
    args = parser.parse_args(argv)
    if args.gen_synth:
        from samples.gen_synth import generate

        path = generate(args.gen_synth, fps=args.fps)
        print(f"已生成: {path}")
        return 0
    parser.print_help()
    print("\n提示：GUI 页面（采集/审核/设置）将在 v0.1 后续任务实现，当前为核心层。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
