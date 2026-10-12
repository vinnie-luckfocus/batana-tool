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
from app.ui.cues import (
    CUE_ARMED,
    CUE_WARN,
    PROMPT_ERROR_CAMERA,
    PROMPT_ERROR_STORAGE,
    TEXT_NO_SWING,
    TEXT_SWING,
)

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
    s.post_save_settle_seconds = 0.0  # 测试：落盘确认立即回 READY，不等沉淀
    return s


@pytest.fixture
def store(settings) -> SessionStore:
    return SessionStore(settings.storage_root)


def _controller(settings, store, present=True, start_at=4, end_at=8):
    return CaptureController(
        settings, store,
        presence=FakePresence(present=present),
        swing=FakeSwing(pre_roll_frames=2, start_at=start_at, end_at=end_at),
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
    from app.ui.cues import error_prompt

    assert error_prompt("UVC 采集失败（设备 index=0 可能已断开）") == PROMPT_ERROR_CAMERA
    assert error_prompt("片段落盘失败: 磁盘已满") == PROMPT_ERROR_STORAGE
    assert error_prompt("未知错误") not in (PROMPT_ERROR_CAMERA, PROMPT_ERROR_STORAGE)


# ---- M3 ARMED 超时视觉提醒 + 能量阈值刻度 ----


def test_armed_shows_swing_cue(qapp, settings, store):
    # 视觉引导：ARMED 即出「挥棒！」大字（armed cue），替代原语音播报
    controller = _controller(settings, store, start_at=None)
    page = CapturePage(settings, store, controller=controller)
    _feed(controller, 3)  # IDLE→READY→（倒计时 2 帧）→ARMED
    assert controller.state_machine.state is State.ARMED
    assert page.preview.cue() == (TEXT_SWING, CUE_ARMED)
    page.shutdown()


def test_armed_timeout_visual_prompt(qapp, settings, store):
    controller = _controller(settings, store, start_at=None)
    page = CapturePage(settings, store, controller=controller)
    page._no_swing_interval_ms = 50
    _feed(controller, 3)  # IDLE→READY→（倒计时 2 帧）→ARMED
    assert controller.state_machine.state is State.ARMED
    QTest.qWait(200)
    assert page.preview.cue() == (TEXT_NO_SWING, CUE_WARN)
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
    page.manual_toggle()  # 空格 = 手动结束 → SAVING（挂起等落盘确认）
    assert controller.state_machine.state is State.SAVING
    assert controller.flush_saves()
    qapp.processEvents()  # worker 落盘完成 → 确认 → READY
    assert controller.state_machine.state is State.READY
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


# ---- 骨架标注效率（2026-10-12：关节识别 / 连续错位传播 / 手部简化）----


def _stub_pose_frames(count: int):
    from app.pose import StubPoseEstimator

    est = StubPoseEstimator()
    return [est.estimate(make_frame(30), i, i / FPS * 1000.0) for i in range(count)]


def test_propagate_correction_stops_on_recovery_and_undo_batch(qapp, settings, store):
    """连续错位一键传播：错位帧应用偏移量；恢复（高置信且脱离错位簇）即停；整批一次撤销。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._pose_frames = _stub_pose_frames(record["frame_count"])  # 6 帧
    # 模拟连续错位：帧 2、3 的左腕（15）跟踪丢失（低置信）
    for f in (2, 3):
        page._pose_frames[f].keypoints[15].visibility = 0.2
    # 帧 4 恢复：高置信且位置跳离错位簇（位移 > 容差 0.08）
    src = page._pose_frames[1].keypoints[15]
    kp4 = page._pose_frames[4].keypoints[15]
    kp4.visibility = 0.9
    kp4.x, kp4.y = src.x + 0.3, src.y + 0.3
    page._frame_pos = 1
    page.correct_keypoint(15, 0.50, 0.50)
    assert page.propagate_correction(1) == 2
    for f in (2, 3):
        kp = page._pose_frames[f].keypoints[15]
        assert kp.manual and kp.auto is not None
    assert page._pose_frames[4].keypoints[15].manual is False  # 恢复即停
    # 整批一次撤销：两帧都回退
    page.undo_correction()
    for f in (2, 3):
        kp = page._pose_frames[f].keypoints[15]
        assert kp.manual is False and kp.visibility == 0.2
    # 无修正源时传播为空操作
    page._last_corrected_kp = None
    assert page.propagate_correction(1) == 0
    page.shutdown()


def test_propagate_correction_stops_at_manual_frame(qapp, settings, store):
    """传播不越过人工修正帧（人工边界优先）。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._pose_frames = _stub_pose_frames(record["frame_count"])
    for f in (2, 3, 4):
        page._pose_frames[f].keypoints[15].visibility = 0.2
    # 帧 3 已是人工帧
    page._pose_frames[3].correct(15, 0.1, 0.1)
    page._frame_pos = 1
    page.correct_keypoint(15, 0.50, 0.50)
    assert page.propagate_correction(1) == 1  # 只传播到帧 2，帧 3 人工帧停止
    assert page._pose_frames[2].keypoints[15].manual is True
    kp3 = page._pose_frames[3].keypoints[15]
    assert (kp3.x, kp3.y) == (0.1, 0.1)  # 人工帧未被覆盖
    page.shutdown()


def test_hover_identifies_keypoint_and_simplify_hands(qapp):
    """悬停显示关节名（_hover 命中 (目, 点)）；简化手部后细节点不命中、渲染不崩。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from app.pose import StubPoseEstimator
    from app.ui.player import HAND_DETAIL_INDICES, PlayerWidget

    w = PlayerWidget()
    w.resize(640, 400)
    w.show()
    frame = make_frame(30, (200, 320))
    pose = StubPoseEstimator().estimate(frame, 0, 0.0)
    w.set_frame_data(frame, pose)
    iw, ih = w.image_size()

    def move_to(kp_idx: int):
        kp = pose.keypoints[kp_idx]
        pos = w.image_to_widget(kp.x * iw, kp.y * ih)
        ev = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(pos),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        w.mouseMoveEvent(ev)

    move_to(15)
    assert w._hover == ("left", 15)
    move_to(25)
    assert w._hover == ("left", 25)

    # 简化手部：细节点（17 左小指）不再命中
    w.set_simplify_hands(True)
    move_to(17)
    assert w._hover is None or w._hover[1] not in HAND_DETAIL_INDICES
    w.grab()  # 渲染冒烟：悬停 + 简化 + 骨架叠加不崩
    w.close()


def test_dual_view_hit_test_and_drag_lock_eye(qapp):
    """双目并排：命中按半幅归目；拖动锁定按下目（跨中线不串目）。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from app.pose import StubPoseEstimator
    from app.ui.player import PlayerWidget

    w = PlayerWidget()
    w.resize(1280, 400)
    w.show()
    left = make_frame(30, (200, 320))
    right = make_frame(60, (200, 320))
    est = StubPoseEstimator()
    pose_l = est.estimate(left, 0, 0.0)
    pose_r = est.estimate(right, 0, 0.0)
    w.set_frame_data_dual(left, right, pose_l, pose_r)
    assert w._dual is True
    iw, ih = w.image_size()
    assert iw == 640  # 双目拼接宽

    def ev(t, pos, btn=Qt.MouseButton.NoButton):
        return QMouseEvent(t, QPointF(pos), btn, btn, Qt.KeyboardModifier.NoModifier)

    # 右目关键点 15 在拼接图中的控件坐标（右半幅偏移 iw/2）
    kp = pose_r.keypoints[15]
    pos_r = w.image_to_widget(iw / 2 + kp.x * iw / 2, kp.y * ih)
    w.mouseMoveEvent(ev(QEvent.Type.MouseMove, pos_r))
    assert w._hover == ("right", 15)

    # 按下右目点 → 拖到左半幅 → 释放：落账仍是右目（锁目不串目）
    moved = []
    w.keypoint_moved.connect(lambda *a: moved.append(a))
    w.mousePressEvent(ev(QEvent.Type.MouseButtonPress, pos_r, Qt.MouseButton.LeftButton))
    w.mouseMoveEvent(ev(QEvent.Type.MouseMove, QPointF(50, 200)))
    w.mouseReleaseEvent(ev(QEvent.Type.MouseButtonRelease, QPointF(50, 200), Qt.MouseButton.LeftButton))
    assert len(moved) == 1
    eye, idx, x, y = moved[0]
    assert eye == "right" and idx == 15
    assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0  # 右目目内归一化坐标
    w.grab()  # 双目渲染冒烟
    w.close()


def test_propagate_covers_high_visibility_stuck_point(qapp, settings, store):
    """实机回归（2026-10-12）：MediaPipe 对错位点也报高置信（实测 vis 0.84-0.93），
    传播停止条件不能只看置信度——高置信但位置卡在错位簇内仍要传播；
    高置信且位移超容差（真正恢复）才停止。传播后视图跳到末帧。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._pose_frames = _stub_pose_frames(record["frame_count"])  # 6 帧
    ax = page._pose_frames[1].keypoints[15].x
    ay = page._pose_frames[1].keypoints[15].y
    # 帧 2、3：高置信但位置卡住（与源点几乎不动）= 高置信错位
    for f in (2, 3):
        kp = page._pose_frames[f].keypoints[15]
        kp.visibility = 0.9
        kp.x, kp.y = ax + 0.01, ay + 0.01
    # 帧 4：高置信且跳离错位簇 = 检测恢复
    kp4 = page._pose_frames[4].keypoints[15]
    kp4.visibility = 0.9
    kp4.x, kp4.y = ax + 0.3, ay + 0.3
    page._frame_pos = 1
    page.correct_keypoint(15, 0.50, 0.50)
    assert page.propagate_correction(1) == 2  # 高置信但卡住的帧 2、3 仍被传播
    assert page._pose_frames[2].keypoints[15].manual is True
    assert page._pose_frames[3].keypoints[15].manual is True
    assert page._pose_frames[4].keypoints[15].manual is False  # 恢复帧不动
    assert page._frame_pos == 3  # 传播后跳到末帧便于检查
    page.shutdown()


def test_repropagate_after_manual_adjust(qapp, settings, store):
    """实机回归（2026-10-12 二次）：传播 → 人工微调其中一帧 → 再次传播，
    之前机器传播的下游帧必须能被新偏移量覆盖（不能被自己的传播结果挡住）。

    规则：机器传播的帧可再传播；人工拖动的帧成为边界（传播到此即停）。
    """
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._pose_frames = _stub_pose_frames(record["frame_count"])  # 6 帧（0-5）
    # 帧 2/3/4 跟踪丢失（低置信），帧 5 恢复（高置信 + 跳离）
    for f in (2, 3, 4):
        page._pose_frames[f].keypoints[15].visibility = 0.2
    src = page._pose_frames[1].keypoints[15]
    kp5 = page._pose_frames[5].keypoints[15]
    kp5.visibility = 0.9
    kp5.x, kp5.y = src.x + 0.3, src.y + 0.3

    # 第一轮：帧 1 修正 → 传播到帧 2/3/4
    page._frame_pos = 1
    page.correct_keypoint(15, 0.50, 0.50)
    assert page.propagate_correction(1) == 3
    assert page._frame_pos == 4  # 跳到传播末帧

    # 人工微调帧 4（传播得不对），帧 4 转为人工边界
    page.correct_keypoint(15, 0.60, 0.60)
    assert (4, 15) not in page._propagated

    # 第二轮：从帧 4 向后传播——下游（帧 5 恢复）即停，无事发生
    assert page.propagate_correction(1) == 0

    # 回到帧 2 人工微调后向后传播：帧 3 是机器传播帧 → 用新偏移量覆盖
    page._frame_pos = 2
    page.correct_keypoint(15, 0.55, 0.55)
    assert page.propagate_correction(1) == 1  # 只覆盖帧 3；帧 4 是人工边界
    kp3 = page._pose_frames[3].keypoints[15]
    src2 = page._pose_frames[2].keypoints[15]
    delta_x = 0.55 - src2.auto["x"]
    assert kp3.manual is True
    assert abs(kp3.x - (kp3.auto["x"] + delta_x)) < 1e-9  # 基于原始自动值 + 新偏移量
    kp4 = page._pose_frames[4].keypoints[15]
    assert (kp4.x, kp4.y) == (0.60, 0.60)  # 人工帧未被覆盖
    page.shutdown()


def test_switch_eye_with_unsaved_pose_shows_correct_eye(qapp, settings, store):
    """换目串骨架回归（2026-10-12）：双目推理后（未保存 dirty）切换查看目，
    必须显示对应目骨架；双目并排视图两目骨架同时传给播放器。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    # 模拟双目推理完成：左右目骨架都在内存且未保存（dirty）
    frames_l = _stub_pose_frames(record["frame_count"])
    frames_r = _stub_pose_frames(record["frame_count"])
    frames_r[0].keypoints[0].x = 0.111  # 标记右目数据
    page._on_pose_done({"left": frames_l, "right": frames_r})
    assert page._pose_dirty is True
    # 切到右目：必须换成右目骨架（dirty 不得阻挡切目）
    page.combo_eye.setCurrentIndex(1)
    assert page._pose_eye == "right"
    assert page._pose_frames[0].keypoints[0].x == 0.111
    # 切回左目：恢复左目骨架（内存字典分目存放，不串目）
    page.combo_eye.setCurrentIndex(0)
    assert page._pose_frames[0].keypoints[0].x != 0.111
    # 双目并排：两目骨架同时传给播放器
    page.combo_eye.setCurrentIndex(2)
    assert set(page.player._pose_by_eye.keys()) == {"left", "right"}
    page.shutdown()
