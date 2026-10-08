"""UX 评审整改（docs/reviews/2026-09-20-ux-review.md）离屏 UI 测试。

覆盖：H1 中文状态映射/分色/倒计时、H2 标记自动跳下一段+触发帧跳转+快捷键、
H3 未保存确认、H4 删除流程、H5 启动 rebuild 提示与异常分类、
M3 ARMED 超时语音、M4 导出磁盘预检、L1 采集快捷键、M3 能量阈值刻度。
"""

from __future__ import annotations

import os
import shutil

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from app.detect import State
from app.session import STATUS_PASS, SessionStore
from app.ui.capture_page import CapturePage
from app.ui.controller import CaptureController
from app.ui.main_window import MainWindow
from app.ui.review_page import ReviewPage
from app.ui.settings import AppSettings
from app.ui.theme import semantic_hex
from app.ui.widgets import STATE_LABELS, MiniBar, StateBanner
from app.voice import PROMPT_ERROR_CAMERA, PROMPT_ERROR_STORAGE, PROMPT_NO_SWING, NullVoice

from conftest import FakePresence, FakeSwing, make_frame
from test_ui_smoke import _write_test_clip

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
    s.voice_enabled = False
    return s


@pytest.fixture
def store(settings) -> SessionStore:
    return SessionStore(settings.storage_root)


def _controller(settings, store, present=True, start_at=4, end_at=8, voice=None):
    return CaptureController(
        settings, store,
        presence=FakePresence(present=present),
        swing=FakeSwing(pre_roll_frames=2, start_at=start_at, end_at=end_at),
        voice=voice or NullVoice(),
    )


def _feed(controller, n: int) -> None:
    for idx in range(n):
        controller.feed(idx, int(idx / FPS * 1e9), make_frame(30), make_frame(30))


# ---- H1 状态横幅中文化 + 分色 + 视觉倒计时 ----


def test_state_banner_chinese_labels(qapp):
    banner = StateBanner()
    for eng, zh in STATE_LABELS.items():
        banner.set_state(eng)
        assert banner.text() == zh
    assert STATE_LABELS["ARMED"] == "请挥棒！"


def test_state_banner_colors(qapp):
    """状态 pill 语义色：绿=就绪、蓝=进行中、红=异常/挥棒提示、灰=空闲。"""
    banner = StateBanner()
    banner.set_state("READY")
    assert semantic_hex("green") in banner.styleSheet()   # 请准备=就绪绿
    banner.set_state("SWING")
    assert semantic_hex("blue") in banner.styleSheet()    # 录制中=进行中蓝
    banner.set_state("ARMED")
    assert semantic_hex("red") in banner.styleSheet()     # 请挥棒=红
    banner.set_state("IDLE")
    assert semantic_hex("fg_dim") in banner.styleSheet()  # 空闲灰
    banner.set_state("ERROR", alarm=True)
    assert banner.text() == "异常"
    assert semantic_hex("red") in banner.styleSheet()


def test_state_banner_countdown(qapp):
    banner = StateBanner("READY")
    banner.set_countdown(3)
    assert banner.text() == "3"
    banner.set_countdown(1)
    assert banner.text() == "1"
    # 离开 READY 倒计时清除
    banner.set_state("ARMED")
    assert banner.text() == "请挥棒！"


# ---- H2 审核效率 ----


def test_mark_auto_selects_next_pending(qapp, settings, store):
    r1 = _write_test_clip(store)
    r2 = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(r1["session_id"])
    page.mark(STATUS_PASS)
    # 标记后自动选中并加载下一条待复核
    assert page._record["session_id"] == r2["session_id"]
    item = page.list_clips.currentItem()
    assert item is not None
    assert item.data(0x0100) == r2["session_id"]  # Qt.ItemDataRole.UserRole
    # 再标记 → 无待复核
    page.mark(STATUS_PASS)
    assert "已无待复核" in page.status_line.text()
    page.shutdown()


def test_jump_to_trigger_frame(qapp, settings, store):
    record = _write_test_clip(store)  # trigger_idx=2
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._frame_pos = 0
    page.jump_to_trigger()
    assert page._frame_pos == 2
    page.shutdown()


def test_review_shortcuts_registered(qapp, settings, store):
    page = ReviewPage(settings, store)
    keys = {s.key().toString() for s in page.findChildren(QShortcut)}
    assert "Shift+Left" in keys and "Shift+Right" in keys
    assert any("Z" in k for k in keys)  # Ctrl+Z / Cmd+Z 撤销
    page.shutdown()


# ---- H3 未保存确认 ----


def _make_dirty_pose(page, record):
    from app.pose import StubPoseEstimator

    estimator = StubPoseEstimator()
    page._pose_frames = [
        estimator.estimate(make_frame(30), i, i / FPS * 1000.0)
        for i in range(record["frame_count"])
    ]
    page._pose_dirty = True


def test_unsaved_confirm_cancel_stays(qapp, settings, store, monkeypatch):
    r1 = _write_test_clip(store)
    r2 = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(r1["session_id"])
    _make_dirty_pose(page, r1)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )
    assert page.load_record(r2["session_id"]) is False
    assert page._record["session_id"] == r1["session_id"]
    assert page._pose_dirty is True
    page.shutdown()


def test_unsaved_confirm_discard_switches(qapp, settings, store, monkeypatch):
    r1 = _write_test_clip(store)
    r2 = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(r1["session_id"])
    _make_dirty_pose(page, r1)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
    )
    assert page.load_record(r2["session_id"]) is True
    assert page._record["session_id"] == r2["session_id"]
    assert not (store.root / r1["clip_dir"] / "pose2d.json").exists()
    page.shutdown()


def test_unsaved_confirm_save_writes_pose(qapp, settings, store, monkeypatch):
    r1 = _write_test_clip(store)
    r2 = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(r1["session_id"])
    _make_dirty_pose(page, r1)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save),
    )
    assert page.load_record(r2["session_id"]) is True
    assert (store.root / r1["clip_dir"] / "pose2d.json").is_file()
    page.shutdown()


# ---- H4 删除素材 ----


def test_delete_current_with_confirm(qapp, settings, store, monkeypatch):
    r1 = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(r1["session_id"])
    # 取消：不删
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    assert page.delete_current() is False
    assert store.get(r1["session_id"])["session_id"] == r1["session_id"]
    # 确认：索引与素材目录一并删除
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    assert page.delete_current() is True
    with pytest.raises(KeyError):
        store.get(r1["session_id"])
    assert not (store.root / r1["clip_dir"]).exists()
    assert page._record is None
    page.shutdown()


# ---- H5 启动重建提示 + 异常分类 ----


def test_startup_rebuild_hint(qapp, settings, store):
    orphan = store.sessions_dir / "sess_orphan01"
    orphan.mkdir(parents=True)
    (orphan / "capture_meta.json").write_text(
        '{"frame_count": 12, "fps": 120.0}', encoding="utf-8"
    )
    window = MainWindow(settings, store)
    assert "已恢复 1 段素材" in window.capture_page.status_line.text()
    window.capture_page.shutdown()
    window.review_page.shutdown()
    window.close()


def test_error_prompt_classification():
    from app.voice import error_prompt

    assert error_prompt("UVC 采集失败（设备 index=0 可能已断开）") == PROMPT_ERROR_CAMERA
    assert error_prompt("片段落盘失败: 磁盘已满") == PROMPT_ERROR_STORAGE
    assert error_prompt("未知错误") not in (PROMPT_ERROR_CAMERA, PROMPT_ERROR_STORAGE)


# ---- M3 ARMED 超时语音 + 能量阈值刻度 ----


def test_armed_timeout_voice_prompt(qapp, settings, store):
    voice = NullVoice()
    controller = _controller(settings, store, start_at=None, voice=voice)
    page = CapturePage(settings, store, controller=controller)
    page._no_swing_interval_ms = 50
    _feed(controller, 3)  # IDLE→READY→（倒计时 2 帧）→ARMED
    assert controller.state_machine.state is State.ARMED
    QTest.qWait(300)
    assert PROMPT_NO_SWING in voice.texts()
    page.shutdown()


def test_energy_bar_threshold(qapp):
    bar = MiniBar(maximum=40.0)
    bar.set_threshold(15.0)
    assert bar._threshold == 15.0
    bar.set_value(20.0)
    bar.resize(100, 10)
    bar.grab()  # offscreen 渲染不崩


# ---- L1 采集快捷键 ----


def test_capture_shortcuts_registered(qapp, settings, store):
    page = CapturePage(settings, store, controller=_controller(settings, store))
    keys = {s.key().toString() for s in page.findChildren(QShortcut)}
    assert "Space" in keys
    assert "D" in keys
    page.shutdown()


def test_space_manual_toggle(qapp, settings, store):
    controller = _controller(settings, store, start_at=None)
    page = CapturePage(settings, store, controller=controller)
    _feed(controller, 3)
    assert controller.state_machine.state is State.ARMED
    page.manual_toggle()  # 空格 = 手动开始
    assert controller.state_machine.state is State.SWING
    page.manual_toggle()  # 空格 = 手动结束 → SAVING → READY
    assert controller.state_machine.state is State.READY
    assert controller.flush_saves()
    page.shutdown()


# ---- M4 导出磁盘预检 ----


def test_export_disk_precheck_aborts(qapp, settings, store, monkeypatch):
    record = _write_test_clip(store)
    store.mark(record["session_id"], STATUS_PASS)
    page = ReviewPage(settings, store)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok),
    )
    real_usage = shutil.disk_usage
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda p: real_usage(p)._replace(free=1),  # 剩余 1 字节 → 必不足
    )
    assert page.export_passed() == []
    assert warnings, "磁盘不足应弹窗中止"
    assert page._export_worker is None  # 未启动导出
    page.shutdown()


# ---- M2 骨架目选择 ----


def test_pose_eye_options(qapp, settings, store):
    page = ReviewPage(settings, store)
    items = [page.combo_pose_eye.itemText(i) for i in range(page.combo_pose_eye.count())]
    assert items == ["左目", "右目", "双目"]
    page.shutdown()
