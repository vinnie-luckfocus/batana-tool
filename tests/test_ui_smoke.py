"""UI 冒烟测试（QT_QPA_PLATFORM=offscreen，无显示环境可跑）。

覆盖：三页可实例化、ROI 框选写入 settings.json、状态机 listener → Qt 信号
到达 UI 计数、拖动修正走核心 PoseFrame.correct、--source 文件回放启动不崩。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from app.capture import ClipWriter
from app.session import SessionStore
from app.ui.capture_page import CapturePage
from app.ui.controller import CaptureController
from app.ui.main_window import MainWindow
from app.ui.review_page import ReviewPage
from app.ui.settings import AppSettings
from app.ui.settings_page import SettingsPage
from app.voice import NullVoice

from conftest import FakePresence, FakeSwing, make_frame

FPS = 10.0


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(["batana-tool-test"])
    yield app


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.storage_root = str(tmp_path / "store")
    s.capture_fps = FPS
    s.countdown_seconds = 0.2
    s.buffer_seconds = 3.0
    s.voice_enabled = False  # 测试不出声
    return s


@pytest.fixture
def store(settings) -> SessionStore:
    return SessionStore(settings.storage_root)


def _fake_controller(settings, store, **kwargs) -> CaptureController:
    return CaptureController(
        settings, store,
        presence=kwargs.pop("presence", FakePresence()),
        swing=kwargs.pop("swing", FakeSwing()),
        voice=NullVoice(),
        **kwargs,
    )


# ---- 三页实例化 ----


def test_three_pages_instantiate(qapp, settings, store):
    window = MainWindow(settings, store)
    assert isinstance(window.capture_page, CapturePage)
    assert isinstance(window.review_page, ReviewPage)
    assert isinstance(window.settings_page, SettingsPage)
    assert window.stack.count() == 3
    window.capture_page.shutdown()
    window.review_page.shutdown()
    window.close()


def test_pages_instantiate_standalone(qapp, settings, store):
    capture = CapturePage(settings, store, controller=_fake_controller(settings, store))
    review = ReviewPage(settings, store)
    settings_page = SettingsPage(settings)
    assert capture.preview is not None
    assert review.list_clips is not None
    assert settings_page.btn_save is not None
    capture.shutdown()
    review.shutdown()


def test_vibrancy_graceful_fallback(qapp):
    """毛玻璃模块：offscreen（无窗口服务器）下优雅回退，不崩、返回 False。"""
    from PySide6.QtWidgets import QWidget

    from app.ui.vibrancy import apply_vibrancy, vibrancy_available

    w = QWidget()
    assert vibrancy_available() is False  # offscreen 平台不可用
    assert apply_vibrancy(w, material="sidebar") is False
    assert apply_vibrancy(w, material="不存在材质") is False


def test_appearance_refresh_restyles(qapp):
    """浅/深色切换：refresh_theme 重挂全局 QSS；自绘件 paletteChange 重取语义色。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QPalette

    from app.ui.theme import apply_theme, refresh_theme
    from app.ui.widgets import StateBanner

    apply_theme(qapp)
    before = qapp.styleSheet()
    # 模拟外观变化（改调色板）后 refresh_theme 重新生成 QSS
    palette = qapp.palette()
    palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Window).darker(101))
    qapp.setPalette(palette)
    refresh_theme(qapp)
    assert qapp.styleSheet()  # 重挂成功
    # 状态 pill：paletteChange 事件后内联样式重算（仍含语义色）
    banner = StateBanner("READY")
    old = banner.styleSheet()
    banner.changeEvent(QEvent(QEvent.Type.PaletteChange))
    assert banner.styleSheet() and "background-color" in banner.styleSheet()
    assert old  # 原样式同样有效（颜色值是否变化取决于调色板亮度跨越）


def test_main_window_minimum_size(qapp, settings, store):
    """最小尺寸约束：1100×680，且最小尺寸下三页可渲染不崩。"""
    window = MainWindow(settings, store)
    assert window.minimumWidth() == 1100
    assert window.minimumHeight() == 680
    window.resize(1100, 680)
    for i in range(3):
        window.stack.setCurrentIndex(i)
        qapp.processEvents()
        window.grab()  # 最小尺寸离屏渲染不崩
    window.capture_page.shutdown()
    window.review_page.shutdown()
    window.close()


# ---- ROI 框选 → settings.json ----


def _mouse(event_type, pos: QPointF, button=Qt.MouseButton.LeftButton) -> QMouseEvent:
    return QMouseEvent(event_type, pos, QPointF(pos), button, button, Qt.KeyboardModifier.NoModifier)


def test_roi_drag_persists_to_settings(qapp, settings, store):
    page = CapturePage(settings, store, controller=_fake_controller(settings, store))
    page.preview.resize(640, 400)
    page.preview.set_frames(make_frame(0, (200, 320)), make_frame(0, (200, 320)))

    w, h = page.preview.width(), page.preview.height()
    p0 = QPointF(w * 0.2, h * 0.2)
    p1 = QPointF(w * 0.7, h * 0.8)
    page.preview.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, p0))
    page.preview.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, p1))
    page.preview.mouseReleaseEvent(
        _mouse(QMouseEvent.Type.MouseButtonRelease, p1, button=Qt.MouseButton.LeftButton)
    )

    # 预览内 ROI 已设置，且已持久化到 settings.json
    roi = page.preview.roi()
    assert roi is not None and roi[2] > 0 and roi[3] > 0
    assert settings.roi_tuple() == roi
    data = json.loads((Path(settings._path)).read_text(encoding="utf-8"))
    assert data["roi"] == list(roi)
    page.shutdown()


# ---- 状态机 listener → Qt 信号 → UI 计数 ----


def test_state_machine_signals_reach_ui(qapp, settings, store):
    presence = FakePresence(present=True)
    swing = FakeSwing(pre_roll_frames=2, start_at=4, end_at=8)
    controller = _fake_controller(settings, store, presence=presence, swing=swing)
    page = CapturePage(settings, store, controller=controller)

    transitions = []
    controller.transitioned.connect(lambda t: transitions.append(t))

    # 9 帧：IDLE→READY→ARMED→SWING→SAVING→READY 一轮完整链路（不进入第二轮）
    for idx in range(9):
        ts = int(idx / FPS * 1e9)
        frame = make_frame(30)
        controller.feed(idx, ts, frame, frame)

    states = [t.next.value for t in transitions]
    # IDLE→READY→ARMED→SWING→SAVING→READY 全链路到达 UI
    assert states[:3] == ["READY", "ARMED", "SWING"]
    assert "SAVING" in states
    # M1：落盘在 worker 线程异步执行，等待队列清空并处理跨线程信号
    assert controller.flush_saves()
    qapp.processEvents()
    # 片段落盘 + 索引登记 → UI 计数更新
    assert store.counts()["total"] == 1
    assert page.m_total.value.text() == "1"
    assert page.m_review.value.text() == "1"
    # 状态横幅跟随状态机且已中文化（保存完成后回 READY→请准备；倒计时中显示大号数字）
    assert page.state_banner.text() in ("请准备", "1")
    page.shutdown()


# ---- 骨架手动修正走核心 API ----


def _write_test_clip(store: SessionStore, frame_count: int = 6, fps: float = FPS) -> dict:
    from app.session import new_session_id

    sid = new_session_id()
    items = [
        (i, int(i / fps * 1e9), make_frame(30), make_frame(31))
        for i in range(frame_count)
    ]
    paths = ClipWriter(fps=fps).write_clip(items, store.sessions_dir / sid)
    return store.add_clip(sid, paths.out_dir, paths.frame_count, fps, trigger_idx=2)


def test_keypoint_correction_uses_core_api(qapp, settings, store):
    from app.pose import StubPoseEstimator, read_pose2d

    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    assert page._player is not None

    # Stub 骨架铺满全段
    estimator = StubPoseEstimator()
    page._pose_frames = [
        estimator.estimate(make_frame(30), i, i / FPS * 1000.0)
        for i in range(record["frame_count"])
    ]
    page._pose_model = StubPoseEstimator.model_name
    page._pose_fps = FPS

    # 拖动修正：走核心 PoseFrame.correct（manual + auto 原值保留）
    page._frame_pos = 2
    before = page._pose_frames[2].keypoints[15]
    auto_x, auto_y = before.x, before.y
    page.correct_keypoint(15, 0.42, 0.24)
    after = page._pose_frames[2].keypoints[15]
    assert after.manual is True
    assert (after.x, after.y) == (0.42, 0.24)
    assert after.auto is not None and after.auto["x"] == auto_x and after.auto["y"] == auto_y

    # 撤销单点修正：恢复自动原值
    page.undo_correction()
    restored = page._pose_frames[2].keypoints[15]
    assert restored.manual is False
    assert (restored.x, restored.y) == (auto_x, auto_y)

    # 再修正并保存 → pose2d.json 回读保持 manual 标记
    page.correct_keypoint(15, 0.5, 0.5)
    assert page.save_pose() is True
    _model, _fps, frames = read_pose2d(store.root / record["clip_dir"] / "pose2d.json")
    kp = frames[2].keypoints[15]
    assert kp.manual is True and (kp.x, kp.y) == (0.5, 0.5)
    page.shutdown()


def test_trim_saved_to_store(qapp, settings, store):
    record = _write_test_clip(store, frame_count=10)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._frame_pos = 2
    page._set_trim_point("start")
    page._frame_pos = 7
    page._set_trim_point("end")
    assert page.save_trim() is True
    assert store.get(record["session_id"])["trim"] == {"start_frame": 2, "end_frame": 7}
    page.shutdown()


# ---- --source 文件回放演示模式（offscreen 启动不崩 + 全链路出段） ----


@pytest.fixture(scope="module")
def synth_video(tmp_path_factory) -> Path:
    from samples.gen_synth import generate

    out = tmp_path_factory.mktemp("synth_ui") / "synth_swing.mkv"
    return generate(
        out, fps=30.0, width=640, height=200, cycles=2,
        empty_s=1.0, walkin_s=2.0, swing_s=0.5, still_s=1.0,
    )


def test_gui_source_demo_boots_offscreen(qapp, settings, synth_video, tmp_path):
    from app.main import run_gui

    settings.capture_fps = 30.0
    settings.countdown_seconds = 0.5
    settings.presence_ratio = 0.02
    settings.motion_trigger_pct = 2.0
    settings.motion_release_pct = 0.8
    settings.pre_roll_seconds = 1.0
    settings.post_roll_seconds = 0.8
    settings.save()

    code = run_gui(source=str(synth_video), settings_path=str(settings._path),
                   auto_quit_ms=7000)
    assert code == 0
    # 演示模式完整跑通：合成视频 → 检测 → 状态机 → 片段落盘登记
    counts = SessionStore(settings.storage_root).counts()
    assert counts["total"] >= 1
