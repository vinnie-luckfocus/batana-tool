"""batana-tool 入口：QApplication + 三页主窗口；同时保留合成素材生成命令。

用法：
    python -m app.main                        # 启动 GUI（默认采集页，UVC 相机源）
    python -m app.main --source video.mkv     # 视频文件回放演示模式（无相机）
    python -m app.main --gen-synth out.mkv    # 生成合成双目测试视频
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batana-tool", description="batana-core 素材采集与标注工具")
    parser.add_argument("--source", metavar="VIDEO", help="视频文件回放源（无相机演示模式，循环播放）")
    parser.add_argument("--settings", metavar="PATH", help="settings.json 路径（默认 ~/.batana-tool/settings.json）")
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
    return run_gui(source=args.source, settings_path=args.settings)


def run_gui(source: str | None = None, settings_path: str | None = None,
            auto_quit_ms: int | None = None) -> int:
    """启动桌面 GUI。source 提供时直接进入文件回放演示模式。

    auto_quit_ms：冒烟测试用，启动后定时自动关窗退出。
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.session import SessionStore
    from app.ui.main_window import MainWindow
    from app.ui.settings import AppSettings
    from app.ui.theme import apply_theme

    qapp = QApplication.instance() or QApplication(sys.argv[:1])
    qapp.setApplicationName("batana-tool")
    apply_theme(qapp)

    settings = AppSettings.load(settings_path)
    store = SessionStore(settings.storage_root)
    window = MainWindow(settings, store, source_path=source)
    window.show()
    if source:
        # 文件回放演示：启动即开始采集流程（预览 + 检测 + 状态机）
        window.capture_page._on_start_clicked()
    if auto_quit_ms is not None:
        QTimer.singleShot(auto_quit_ms, window.close)
        QTimer.singleShot(auto_quit_ms + 500, qapp.quit)
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())
