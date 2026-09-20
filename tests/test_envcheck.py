"""环境合规主动判定（F11）无头测试。

覆盖：8 项 check 的合成用例（暗/亮/过曝、100Hz 正弦频闪、高斯模糊、倾斜直线、
移动背景、人形 bbox 过紧/过远/出界、掉帧时间戳、mock 磁盘）、runner 集成
（FileSource + 合成视频 → 报告结构/overall 规则/JSON 往返）、UI 离屏冒烟
（EnvCheckDialog 实例化 + 模拟报告渲染 + 设置页环境检查区保存）。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.envcheck import (
    CheckResult,
    EnvCheckSettings,
    EnvironmentChecker,
    EnvironmentReport,
    checks,
)
from app.envcheck.models import (
    OVERALL_FAIL,
    OVERALL_OK,
    OVERALL_WARN,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
)

# ============================================================
# 1 光照充足性
# ============================================================


def test_brightness_dark_fails():
    r = checks.check_brightness([40.0] * 10)
    assert r.status == STATUS_FAIL
    assert "补光" in r.suggestion


def test_brightness_dim_warns():
    r = checks.check_brightness([75.0] * 10)
    assert r.status == STATUS_WARN
    assert "偏暗" in r.suggestion


def test_brightness_normal_passes():
    r = checks.check_brightness([120.0] * 10)
    assert r.status == STATUS_PASS


def test_brightness_overexposed_warns():
    r = checks.check_brightness([210.0] * 10, [0.08] * 10)
    assert r.status == STATUS_WARN
    assert "过曝" in r.suggestion


def test_brightness_bright_without_highlights_passes():
    assert checks.check_brightness([210.0] * 10, [0.01] * 10).status == STATUS_PASS
    # 无高光数据时按亮度区间判定
    assert checks.check_brightness([210.0] * 10).status == STATUS_PASS


def test_brightness_thresholds_parameterized():
    r = checks.check_brightness([75.0] * 10, fail_below=50.0, warn_below=70.0)
    assert r.status == STATUS_PASS


# ============================================================
# 2 频闪
# ============================================================

_FPS = 1000.0


def _sine_means(amp: float, freq: float, n: int = 2000, base: float = 150.0) -> np.ndarray:
    t = np.arange(n) / _FPS
    return base + amp * np.sin(2 * np.pi * freq * t)


def test_flicker_stable_passes():
    assert checks.check_flicker([150.0] * 500, _FPS).status == STATUS_PASS


def test_flicker_mild_warns():
    # 波动 2×2.6/150 ≈ 3.5% → warn
    r = checks.check_flicker(_sine_means(2.6, 100.0), _FPS)
    assert r.status == STATUS_WARN


def test_flicker_strong_fails_and_names_mains():
    # 波动 2×30/150 = 40% → fail，且 100Hz 主频 → 点名市电频闪特征
    r = checks.check_flicker(_sine_means(30.0, 100.0), _FPS)
    assert r.status == STATUS_FAIL
    assert "无频闪 LED" in r.suggestion
    assert "市电频闪特征" in r.suggestion


def test_flicker_120hz_detected():
    r = checks.check_flicker(_sine_means(30.0, 120.0), _FPS)
    assert r.status == STATUS_FAIL
    assert "120Hz" in r.suggestion


def test_flicker_non_mains_freq_not_named():
    # 3Hz 缓慢波动（人走动阴影级别）→ 不点名市电
    r = checks.check_flicker(_sine_means(30.0, 3.0), _FPS)
    assert r.status == STATUS_FAIL
    assert "市电" not in r.suggestion


def test_flicker_too_few_frames_skips():
    assert checks.check_flicker([150.0], _FPS).status == STATUS_SKIP


# ============================================================
# 3 清晰度
# ============================================================


@pytest.fixture
def noise_frame() -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (100, 160), dtype=np.uint8)


def test_sharpness_blurry_warns(noise_frame):
    blurry = cv2.GaussianBlur(noise_frame, (15, 15), 0)
    r = checks.check_sharpness([blurry] * 5)
    assert r.status == STATUS_WARN
    assert "对焦" in r.suggestion


def test_sharpness_crisp_passes(noise_frame):
    assert checks.check_sharpness([noise_frame] * 5).status == STATUS_PASS


# ============================================================
# 4 背景干扰
# ============================================================

_ROI = (50, 20, 100, 60)  # (x, y, w, h)，100x200 画面中央


def _moving_background_frames(n: int = 8) -> list[np.ndarray]:
    """ROI 外底部有大面积移动色块的帧序列。"""
    frames = []
    for i in range(n):
        f = np.full((100, 200), 30, np.uint8)
        cv2.rectangle(f, (i * 10, 82), (i * 10 + 100, 99), 220, -1)
        frames.append(f)
    return frames


def test_background_still_passes():
    frames = [np.full((100, 200), 30, np.uint8) for _ in range(6)]
    assert checks.check_background_motion(frames, _ROI).status == STATUS_PASS


def test_background_motion_warns():
    r = checks.check_background_motion(_moving_background_frames(), _ROI)
    assert r.status == STATUS_WARN
    assert "背景" in r.suggestion


def test_background_motion_inside_roi_ignored():
    """运动发生在 ROI 内 → 不计背景干扰。"""
    frames = []
    for i in range(8):
        f = np.full((100, 200), 30, np.uint8)
        cv2.rectangle(f, (60 + i * 5, 30), (90 + i * 5, 70), 200, -1)
        frames.append(f)
    assert checks.check_background_motion(frames, _ROI).status == STATUS_PASS


# ============================================================
# 5 相机水平
# ============================================================


def _tilted_frame(deg: float) -> np.ndarray:
    """画三条平行直线，倾角 deg。"""
    img = np.zeros((200, 320), np.uint8)
    a = math.radians(deg)
    for y0 in (40, 100, 160):
        x0, x1 = 10, 310
        cv2.line(img, (x0, y0), (x1, int(y0 + math.tan(a) * (x1 - x0))), 200, 2)
    return img


def test_level_horizontal_passes():
    assert checks.check_level([_tilted_frame(0.0)]).status == STATUS_PASS


def test_level_slight_tilt_warns():
    assert checks.check_level([_tilted_frame(3.0)]).status == STATUS_WARN


def test_level_bad_tilt_fails():
    r = checks.check_level([_tilted_frame(8.0)])
    assert r.status == STATUS_FAIL
    assert "调平" in r.suggestion


def test_level_no_lines_skips():
    assert checks.check_level([np.zeros((200, 320), np.uint8)]).status == STATUS_SKIP


# ============================================================
# 6 构图覆盖
# ============================================================

_FROI = (100, 50, 400, 300)  # ROI (x, y, w, h)


def test_framing_good_passes():
    assert checks.check_framing([(200, 100, 100, 180)] * 5, _FROI).status == STATUS_PASS


def test_framing_tight_warns():
    # 头顶距 ROI 上边缘 5px < 5%×300=15px → 构图过紧
    r = checks.check_framing([(200, 55, 100, 180)] * 5, _FROI)
    assert r.status == STATUS_WARN
    assert "构图过紧" in r.suggestion


def test_framing_too_far_warns():
    # 人形高 100 < 40%×300=120 → 距离过远
    r = checks.check_framing([(200, 150, 80, 100)] * 5, _FROI)
    assert r.status == STATUS_WARN
    assert "距离过远" in r.suggestion


def test_framing_out_of_roi_fails():
    r = checks.check_framing([(450, 100, 120, 180)] * 5, _FROI)
    assert r.status == STATUS_FAIL
    assert "未覆盖人体" in r.suggestion


def test_framing_no_person_skips():
    assert checks.check_framing([None] * 5, _FROI).status == STATUS_SKIP


# ============================================================
# 7 帧率/掉帧
# ============================================================


def _timestamps(n: int, fps: float = 120.0) -> list[int]:
    return [int(i / fps * 1e9) for i in range(n)]


def test_framerate_perfect_passes():
    assert checks.check_framerate(_timestamps(1000), 120.0).status == STATUS_PASS


def test_framerate_minor_drops_warns():
    ts = _timestamps(1000)
    ts[500] += int(2 / 120.0 * 1e9)  # 1 处掉帧 → 掉帧率 0.1% → warn
    r = checks.check_framerate(ts, 120.0)
    assert r.status == STATUS_WARN
    assert "掉帧" in r.measured


def test_framerate_heavy_drops_fails():
    ts = _timestamps(500)
    for i in range(50, 500, 50):  # 9 处掉帧 → 掉帧率 1.8% → fail
        ts[i] += int(2 / 120.0 * 1e9)
    assert checks.check_framerate(ts, 120.0).status == STATUS_FAIL


# ============================================================
# 8 磁盘空间（mock shutil.disk_usage）
# ============================================================


class _Usage:
    def __init__(self, free_bytes: int) -> None:
        self.free = free_bytes


def test_disk_insufficient_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: _Usage(int(10e9)))
    assert checks.check_disk(tmp_path, planned_clips=200, est_mb_per_clip=500).status == STATUS_FAIL


def test_disk_tight_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: _Usage(int(150e9)))
    assert checks.check_disk(tmp_path, planned_clips=200, est_mb_per_clip=500).status == STATUS_WARN


def test_disk_plenty_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: _Usage(int(1e12)))
    assert checks.check_disk(tmp_path, planned_clips=200, est_mb_per_clip=500).status == STATUS_PASS


def test_disk_nonexistent_path_probes_parent(monkeypatch, tmp_path):
    """存储根目录未创建时向上找已存在父目录，不报 skip。"""
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: _Usage(int(1e12)))
    missing = tmp_path / "a" / "b" / "c"
    assert checks.check_disk(missing, planned_clips=10, est_mb_per_clip=10).status == STATUS_PASS


# ============================================================
# 报告模型：overall 规则与 JSON 往返
# ============================================================


def _report(*statuses: str) -> EnvironmentReport:
    return EnvironmentReport(
        results=[CheckResult(f"c{i}", f"检查{i}", s, "实测", "建议") for i, s in enumerate(statuses)],
        duration_s=10.0,
        elapsed_s=10.5,
    )


def test_overall_rules():
    assert _report(STATUS_PASS, STATUS_PASS).overall == OVERALL_OK
    assert _report(STATUS_PASS, STATUS_SKIP).overall == OVERALL_OK
    assert _report(STATUS_PASS, STATUS_WARN).overall == OVERALL_WARN
    assert _report(STATUS_WARN, STATUS_FAIL).overall == OVERALL_FAIL


def test_report_json_roundtrip(tmp_path):
    report = _report(STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIP)
    report.created_at = "2026-09-20T12:00:00"
    path = report.save(tmp_path)
    assert path.parent.name == "env_reports"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["overall"] == OVERALL_FAIL
    assert len(data["results"]) == 4
    restored = EnvironmentReport.from_dict(data)
    assert restored.overall == report.overall
    assert [r.status for r in restored.results] == [STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIP]
    assert restored.results[0].name == "检查0"


def test_env_settings_from_app_settings():
    from app.ui.settings import AppSettings

    s = AppSettings()
    s.env_brightness_fail = 55.0
    s.env_planned_clips = 100
    cfg = EnvCheckSettings.from_app_settings(s)
    assert cfg.brightness_fail == 55.0
    assert cfg.planned_clips == 100
    assert cfg.flicker_warn_pct == s.env_flicker_warn_pct


# ============================================================
# runner 集成：FileSource + 合成视频
# ============================================================


@pytest.fixture(scope="module")
def synth_video(tmp_path_factory) -> Path:
    from samples.gen_synth import generate

    out = tmp_path_factory.mktemp("env_synth") / "env_synth.mkv"
    return generate(
        out, fps=30.0, width=640, height=200, cycles=1,
        empty_s=1.0, walkin_s=2.0, swing_s=0.5, still_s=1.0,
    )


def test_runner_report_structure(synth_video, tmp_path):
    from app.capture import FileSource

    source = FileSource(synth_video)
    checker = EnvironmentChecker(source, report_root=tmp_path)
    progress: list[tuple[int, int, str]] = []
    report = checker.run(duration_s=2.0, progress_cb=lambda d, t, n: progress.append((d, t, n)))
    source.close()

    # 8 项检查齐全、ID 唯一、进度回调逐项到达
    assert len(report.results) == 8
    ids = [r.check_id for r in report.results]
    assert ids == ["brightness", "flicker", "sharpness", "bg_motion",
                   "level", "framing", "framerate", "disk"]
    assert [p[0] for p in progress] == list(range(1, 9))
    assert all(r.status in (STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIP) for r in report.results)
    # overall 与逐项结果一致（复用规则）
    assert report.overall == EnvironmentReport(results=report.results).overall
    # 文件源时间戳精确 → 帧率检查应 pass
    framerate = next(r for r in report.results if r.check_id == "framerate")
    assert framerate.status == STATUS_PASS
    # 报告已落盘且 JSON 往返一致
    assert report.report_path and Path(report.report_path).is_file()
    data = json.loads(Path(report.report_path).read_text(encoding="utf-8"))
    assert EnvironmentReport.from_dict(data).overall == report.overall


def test_runner_cli_main(synth_video, tmp_path, capsys):
    from app.envcheck.__main__ import main

    code = main(["--source", str(synth_video), "--duration", "1.5",
                 "--report-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "环境体检报告" in out
    assert "结论" in out
    assert code in (0, 1, 2)


# ============================================================
# UI 冒烟（offscreen）：EnvCheckDialog + 设置页环境检查区
# ============================================================


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["batana-tool-envcheck-test"])
    yield app


def _fake_report() -> EnvironmentReport:
    return EnvironmentReport(
        results=[
            CheckResult("brightness", "光照充足性", STATUS_PASS, "ROI 灰度均值 142", ""),
            CheckResult("flicker", "照明频闪", STATUS_WARN, "帧均值波动 3.20%", "存在轻微频闪"),
            CheckResult("level", "相机水平", STATUS_FAIL, "主水平线倾角 +6.10°", "请调平"),
            CheckResult("framing", "构图覆盖", STATUS_SKIP, "未检测到人体", ""),
        ],
        duration_s=10.0,
        elapsed_s=10.2,
    )


def test_envcheck_dialog_renders_report(qapp, tmp_path):
    from app.ui.envcheck_dialog import EnvCheckDialog, _STATUS_COLORS

    # checker 仅作占位（不启动 worker），对话框可独立实例化
    checker = EnvironmentChecker.__new__(EnvironmentChecker)
    dialog = EnvCheckDialog(checker, duration_s=10.0)
    report = _fake_report()
    dialog.show_report(report)

    assert dialog.table.rowCount() == 4
    # 状态色块与文案
    from PySide6.QtGui import QColor

    cell = dialog.table.item(0, 0)
    assert cell.text() == "合格"
    assert cell.background().color() == QColor(_STATUS_COLORS[STATUS_PASS])
    assert dialog.table.item(1, 0).text() == "警告"
    assert dialog.table.item(2, 0).text() == "不合格"
    assert dialog.table.item(3, 0).text() == "跳过"
    assert dialog.table.item(2, 3).text() == "请调平"
    # overall 大字结论（有 fail → 不可采集）
    assert dialog.label_overall.text() == OVERALL_FAIL


def test_capture_page_envcheck_button_and_monitor(qapp, tmp_path):
    from app.session import SessionStore
    from app.ui.capture_page import CapturePage
    from app.ui.controller import CaptureController
    from app.ui.settings import AppSettings
    from app.voice import NullVoice

    from conftest import FakePresence, FakeSwing, make_frame

    settings = AppSettings()
    settings._path = tmp_path / "settings.json"
    settings.storage_root = str(tmp_path / "store")
    settings.voice_enabled = False
    store = SessionStore(settings.storage_root)
    controller = CaptureController(
        settings, store, presence=FakePresence(), swing=FakeSwing(), voice=NullVoice()
    )
    page = CapturePage(settings, store, controller=controller)
    assert "环境体检" in page.btn_envcheck.text()

    # 监测仅在采集中进行：mock running=True（无头测试不起抓帧线程）
    from unittest.mock import PropertyMock, patch

    running = patch.object(type(controller), "running", new_callable=PropertyMock)
    running.return_value = True
    with running:
        # 喂暗帧 → evaluate_environment 报亮度不足 → 横幅提示
        # （_active_fps=120 → 监测窗口 30 个样本，每 4 帧 1 样本，需 ≥120 帧）
        dark = make_frame(20, (200, 320))
        for idx in range(200):
            controller.feed(idx, int(idx / 10 * 1e9), dark, dark)
        page._monitor_env()
        assert "光照" in page.env_banner.text()

        # 恢复明亮帧 → 横幅隐藏
        bright = make_frame(150, (200, 320))
        for idx in range(200, 400):
            controller.feed(idx, int(idx / 10 * 1e9), bright, bright)
        page._monitor_env()
        assert page.env_banner.text() == "" or not page.env_banner.isVisible()
    page.shutdown()


def test_settings_page_env_section_roundtrip(qapp, tmp_path):
    from app.ui.settings import AppSettings
    from app.ui.settings_page import SettingsPage

    settings = AppSettings()
    settings._path = tmp_path / "settings.json"
    page = SettingsPage(settings)
    page.spin_env_bright_fail.setValue(55)
    page.spin_env_flicker_fail.setValue(4.0)
    page.spin_env_clips.setValue(120)
    page.save()
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["env_brightness_fail"] == 55.0
    assert data["env_flicker_fail_pct"] == 4.0
    assert data["env_planned_clips"] == 120
