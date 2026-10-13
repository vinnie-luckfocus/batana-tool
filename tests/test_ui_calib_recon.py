"""标定与 3D 重建的离屏 UI 测试：设置页标定区块、审核页 3D 重建按钮逻辑。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

import app.calib.store as calib_store
import app.ui.settings_page as settings_page_mod
from app.pose import StubPoseEstimator, write_pose2d
from app.session import SessionStore
from app.ui.review_page import ReviewPage
from app.ui.settings import AppSettings
from app.ui.settings_page import SettingsPage

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


@pytest.fixture
def calib_path(tmp_path, monkeypatch):
    """标定存档重定向到临时目录（不碰真实 ~/.batana-tool/calibration.json）。"""
    path = tmp_path / "calibration.json"
    monkeypatch.setattr(calib_store, "DEFAULT_CALIBRATION_PATH", path)
    monkeypatch.setattr(settings_page_mod, "DEFAULT_CALIBRATION_PATH", path)
    return path


def test_settings_page_calib_section(qapp, settings, calib_path, monkeypatch):
    """标定区块存在：状态行三态切换（无 → 名义 → 清除）。"""
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    page = SettingsPage(settings)
    assert page.label_calib_status.text() == "无标定"
    assert page.btn_nominal_calib.isEnabled()
    assert page.btn_clear_calib.isEnabled()

    page.btn_nominal_calib.click()  # 使用名义标定（仅冒烟）
    assert calib_path.is_file()
    assert "名义标定" in page.label_calib_status.text()

    page.btn_clear_calib.click()
    assert not calib_path.exists()
    assert page.label_calib_status.text() == "无标定"


def test_settings_page_calibrate_button_core_availability(qapp, settings, calib_path, monkeypatch):
    """core 仓不可用时标定按钮禁用并提示。"""
    page = SettingsPage(settings)
    import app.session.export as export_mod

    monkeypatch.setattr(settings_page_mod, "resolve_core_repo", lambda _c="": None)
    page._refresh_calib_status()
    assert not page.btn_calibrate.isEnabled()
    assert "core" in page.btn_calibrate.toolTip().lower()
    monkeypatch.setattr(
        settings_page_mod, "resolve_core_repo", export_mod.resolve_core_repo
    )
    page._refresh_calib_status()
    assert page.btn_calibrate.isEnabled()  # 本机存在 batana-core 仓


def _write_pose(clip_dir, eye: str) -> None:
    from app.ui.review_page import pose_path_for_eye

    est = StubPoseEstimator()
    frames = [est.estimate(make_frame(30), i, i / FPS * 1000.0) for i in range(6)]
    write_pose2d(pose_path_for_eye(clip_dir, eye), est.model_name, FPS, frames)


def test_review_page_recon3d_button_enable(qapp, settings, store):
    """3D 重建按钮：双骨架齐全才可用；缺右目时禁用且 tooltip 提示原因。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    assert not page.btn_recon3d.isEnabled()  # 未加载素材

    page.load_record(record["session_id"])
    assert not page.btn_recon3d.isEnabled()  # 无骨架
    clip_dir = store.root / record["clip_dir"]

    _write_pose(clip_dir, "left")
    page._update_recon3d_button()
    assert not page.btn_recon3d.isEnabled()
    assert "右目" in page.btn_recon3d.toolTip()

    _write_pose(clip_dir, "right")
    page._update_recon3d_button()
    assert page.btn_recon3d.isEnabled()
    page.shutdown()


def test_review_page_recon3d_button_updates_after_save_pose(qapp, settings, store):
    """保存骨架后按钮状态即时刷新（无需重载素材）。"""
    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    est = StubPoseEstimator()
    frames_by_eye = {
        eye: [est.estimate(make_frame(30), i, i / FPS * 1000.0) for i in range(6)]
        for eye in ("left", "right")
    }
    page._on_pose_done(frames_by_eye)
    assert not page.btn_recon3d.isEnabled()  # 内存有但未落盘
    assert page.save_pose() is True
    assert page.btn_recon3d.isEnabled()
    page.shutdown()
