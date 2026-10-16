"""M-A/M-B/M-C UI 工作流测试：建议写 trim、范围跑骨架合并、传播 trim 边界、导出按钮。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from app.pose import StubPoseEstimator, write_pose2d
from app.session import SessionStore
from app.ui.review_page import ReviewPage, _PoseWorker, pose_path_for_eye
from app.ui.settings import AppSettings

from conftest import make_frame
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
    return s


@pytest.fixture
def store(settings) -> SessionStore:
    return SessionStore(settings.storage_root)


def _stub_frames(count: int):
    est = StubPoseEstimator()
    return [est.estimate(make_frame(30), i, i / FPS * 1000.0) for i in range(count)]


def test_suggest_trim_button_writes_trim(qapp, settings, store):
    """挥棒段建议：写入 trim（与手动同一更新路径），播放头跳到起点，标脏待保存。"""
    record = _write_test_clip(store)  # 6 帧，fps=10
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    page._record["trigger_idx"] = 4  # 基础窗口 [4−1.5, 4+4] → [2, 8] → clamp [2, 5]
    page.suggest_trim()
    assert page._record["trim"] == {"start_frame": 2, "end_frame": 5}
    assert page._trim_dirty is True
    assert page.label_trim.text() == "[2, 5]"
    assert page._frame_pos == 2
    assert "触发帧" in page.status_line.text()
    # 保存后落盘
    assert page.save_trim() is True
    assert store.get(record["session_id"])["trim"] == {"start_frame": 2, "end_frame": 5}
    # 已有非整段 trim → 范围默认「仅 trim 段」
    page2 = ReviewPage(settings, store)
    page2.load_record(record["session_id"])
    assert page2.combo_pose_range.currentIndex() == 1
    page.shutdown()
    page2.shutdown()


def test_pose_worker_range_and_merge(qapp, settings, store):
    """范围跑骨架：只推理 trim 段内帧；合并后范围外已有数据保留。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    n = record["frame_count"]
    old_left = _stub_frames(n)
    old_right = _stub_frames(n)
    page._pose_frames_by_eye = {"left": list(old_left), "right": old_right}

    worker = _PoseWorker(StubPoseEstimator, page._player, ["left"],
                         StubPoseEstimator.model_name, frame_range=(2, 3))
    results: dict = {}
    worker.finished_ok.connect(lambda d: results.update(d))
    worker.run()
    assert [f.frame_index for f in results["left"]] == [2, 3]  # 只推理范围内

    page._on_pose_done(results)
    merged = page._pose_frames_by_eye["left"]
    assert len(merged) == n  # 范围外旧数据保留
    assert [f.frame_index for f in merged] == list(range(n))
    assert merged[0] is old_left[0]
    assert merged[2] is results["left"][0]  # 范围内用新结果
    assert page._pose_frames_by_eye["right"] is old_right  # 另一目未跑不动
    page.shutdown()


def test_propagate_respects_trim_boundary(qapp, settings, store):
    """传播修正在 trim 边界停止：trim [2,4]，向后传播不越 4，向前不越 2。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    frames = _stub_frames(record["frame_count"])
    page._pose_frames = frames
    for f in frames:
        f.keypoints[15].visibility = 0.2  # 全部低置信（无恢复帧）
    page._record["trim"] = {"start_frame": 2, "end_frame": 4}
    page._frame_pos = 1
    page.correct_keypoint(15, 0.50, 0.50)
    assert page.propagate_correction(1) == 3  # 只覆盖帧 2/3/4
    assert frames[5].keypoints[15].manual is False  # 越界帧不动
    for f in (2, 3, 4):
        assert frames[f].keypoints[15].manual is True
    # 向前传播：源帧 5 修正后向前传，停在 trim 下界 2
    page._frame_pos = 5
    page.correct_keypoint(15, 0.60, 0.60)
    assert page.propagate_correction(-1) == 3  # 帧 4/3/2
    assert frames[1].keypoints[15].x == 0.50  # 帧 1 保持源修正值，未被覆盖
    page.shutdown()


def test_export_training_button_enable_and_export(qapp, settings, store, monkeypatch):
    """导出训练包按钮：无骨架禁用；有骨架可用并实际导出。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    assert not page.btn_export_training.isEnabled()

    clip_dir = store.root / record["clip_dir"]
    write_pose2d(pose_path_for_eye(clip_dir, "left"), "stub", FPS,
                 _stub_frames(record["frame_count"]))
    page._update_training_button()
    assert page.btn_export_training.isEnabled()

    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: infos.append(a) or QMessageBox.StandardButton.Ok),
    )
    page.export_training()
    out = store.root / "training" / record["session_id"]
    assert (out / "annotations.json").is_file()
    assert (out / "meta.json").is_file()
    assert infos, "导出完成应弹报告"
    page.shutdown()
