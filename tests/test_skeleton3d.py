"""3D 骨架查看器测试：相机数学、数据转换、UI smoke、审核页入口与帧同步。"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.pose.model import KEYPOINT_NAMES
from app.ui.skeleton3d_math import (
    SKELETON3D_BONES,
    OrbitCamera,
    estimate_ground_y,
    perspective,
    pose3d_frames_to_arrays,
    pose3d_to_display,
    project_points,
)

FPS = 10.0


def _make_pose3d(n_frames: int = 6, masked: dict[int, set[int]] | None = None) -> dict:
    """假 pose3d：站立人形（米，左目系 y 朝下），可按帧掩蔽关节。"""
    frames = []
    for i in range(n_frames):
        joints = []
        for k, name in enumerate(KEYPOINT_NAMES):
            if masked and i in masked and k in masked[i]:
                joints.append({"name": name, "x": None, "y": None, "z": None,
                               "visibility": 0.0})
                continue
            joints.append({
                "name": name,
                "x": (k % 5 - 2) * 0.15,
                "y": 0.4 + (k // 5) * 0.25,  # 左目系 y 朝下：头小值、脚大值
                "z": 2.0,
                "visibility": 0.9,
            })
        frames.append({"frame_index": i, "timestamp_ms": i / FPS * 1000.0,
                       "t_ms": i / FPS * 1000.0, "confidence": 0.9, "joints": joints})
    return {"model": "stub", "frame_rate": FPS, "unit": "m", "frames": frames}


# ---- 数据转换 ----


def test_frames_to_arrays_marks_masked_nan():
    pose3d = _make_pose3d(3, masked={1: {15}})
    indices, pts = pose3d_frames_to_arrays(pose3d)
    assert indices == [0, 1, 2]
    assert pts.shape == (3, 33, 3)
    assert np.isnan(pts[1, 15]).all()          # 掩蔽关节 → NaN
    assert np.isfinite(pts[1, 14]).all()
    assert pts[0, 15, 2] == pytest.approx(2.0)


def test_display_conversion_y_up():
    """左目系 y 朝下 → 显示系 y 朝上：脚踝应低于头（显示系）。"""
    pose3d = _make_pose3d(1)
    _, pts = pose3d_frames_to_arrays(pose3d)
    ground = estimate_ground_y(pts)
    disp = pose3d_to_display(pts, ground)
    nose_y = disp[0, 0, 1]   # nose
    ankle_y = disp[0, 27, 1]  # left_ankle
    assert ankle_y < nose_y   # 显示系：脚在下
    assert ankle_y == pytest.approx(0.0, abs=0.3)  # 脚踩地面附近


def test_estimate_ground_y_fallback_without_feet():
    pts = np.full((2, 33, 3), np.nan)
    pts[..., 1] = 1.0
    pts[0, 0] = [0.0, 1.2, 2.0]  # 只有头有效
    assert estimate_ground_y(pts) == pytest.approx(1.2)


def test_bones_table_covers_topology():
    assert (11, 12) in SKELETON3D_BONES   # 肩线
    assert (25, 27) in SKELETON3D_BONES   # 左小腿
    assert (27, 31) in SKELETON3D_BONES   # 踝-脚尖
    assert all(0 <= i < 33 and 0 <= j < 33 for i, j in SKELETON3D_BONES)


# ---- 相机数学 ----


def test_orbit_rotates_eye_and_clamps_pitch():
    cam = OrbitCamera(target=(0, 1, 0), distance=3.0)
    eye0 = cam.eye().copy()
    cam.orbit(90.0, 0.0)
    eye1 = cam.eye()
    assert not np.allclose(eye0, eye1)          # 环绕后相机位置改变
    assert np.linalg.norm(eye1 - cam.target) == pytest.approx(3.0)  # 距离不变
    cam.orbit(360.0, 0.0)
    assert np.allclose(cam.eye(), eye1, atol=1e-6)  # 360° 回到原位
    cam.orbit(0.0, 200.0)
    assert cam.pitch_deg == cam.MAX_PITCH_DEG   # 俯仰钳制
    cam.orbit(0.0, -400.0)
    assert cam.pitch_deg == -cam.MAX_PITCH_DEG


def test_zoom_distance_clamped():
    cam = OrbitCamera(distance=3.0)
    cam.zoom(0.5)
    assert cam.distance == pytest.approx(1.5)
    for _ in range(100):
        cam.zoom(0.1)
    assert cam.distance == cam.MIN_DISTANCE
    for _ in range(200):
        cam.zoom(2.0)
    assert cam.distance == cam.MAX_DISTANCE


def test_pan_moves_target():
    cam = OrbitCamera(target=(0, 1, 0), distance=3.0, yaw_deg=0.0)
    cam.pan(0.5, 0.2)
    assert cam.target[0] == pytest.approx(0.5)
    assert cam.target[1] > 1.0  # 屏幕上拖 → target 上移（世界 y 朝上）


def test_projection_center_and_rotation_direction():
    """target 投影到屏幕中心；yaw +90° 后，target 前方固定点移到屏幕另一侧。"""
    w, h = 800, 600
    cam = OrbitCamera(target=(0, 1, 0), distance=3.0, yaw_deg=0.0, pitch_deg=0.0)
    view = cam.view_matrix()
    proj = cam.projection_matrix(w / h)
    px, depth = project_points(np.array([[0.0, 1.0, 0.0]]), view, proj, w, h)
    assert px[0, 0] == pytest.approx(w / 2, abs=1.0)
    assert px[0, 1] == pytest.approx(h / 2, abs=1.0)
    assert depth[0] < 0  # 相机系 -z 朝前

    side = np.array([[0.5, 1.0, 0.0]])  # target 右侧固定点
    px_a, _ = project_points(side, view, proj, w, h)
    cam.orbit(90.0, 0.0)
    px_b, _ = project_points(side, cam.view_matrix(), proj, w, h)
    assert px_a[0, 0] > w / 2   # 初始在中心右侧
    assert abs(px_b[0, 0] - w / 2) < abs(px_a[0, 0] - w / 2)  # 旋转 90° 后回到中线附近


def test_camera_fit_frames_skeleton():
    rng = np.random.default_rng(0)
    pts = rng.uniform(-0.5, 0.5, (33, 3)) + np.array([0.0, 1.0, 2.0])
    cam = OrbitCamera()
    cam.fit(pts)
    assert np.allclose(cam.target, pts.mean(axis=0), atol=0.35)
    # 取景后所有点应投在视口内
    view, proj = cam.view_matrix(), cam.projection_matrix(4 / 3)
    px, depth = project_points(pts, view, proj, 800, 600)
    assert (depth < 0).all()
    assert (px[:, 0] > 0).all() and (px[:, 0] < 800).all()
    assert (px[:, 1] > 0).all() and (px[:, 1] < 600).all()


def test_project_nan_passthrough():
    cam = OrbitCamera()
    px, depth = project_points(np.array([[np.nan, 0, 0], [0, 0, 3.0]]),
                               cam.view_matrix(), cam.projection_matrix(1.0), 100, 100)
    assert np.isnan(px[0]).all()
    assert np.isfinite(px[1]).all()


# ---- UI smoke ----


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["batana-tool-test"])
    yield app


def test_viewer3d_offscreen_smoke(qapp, tmp_path):
    """offscreen 创建查看器并载入数据；GL 上下文不可用时 skip（不判失败）。"""
    from PySide6.QtTest import QTest

    from app.ui.skeleton3d import Skeleton3DDialog

    dlg = Skeleton3DDialog()
    path = tmp_path / "pose3d.json"
    path.write_text(json.dumps(_make_pose3d()), encoding="utf-8")
    dlg.load_pose3d_file(path, FPS)
    assert dlg.viewer.frame_count == 6
    dlg.sync_frame(3)
    assert dlg.viewer.current_frame_index == 3
    dlg.show()
    QTest.qWait(150)
    qapp.processEvents()
    if not dlg.viewer._gl_ok:
        dlg.close()
        pytest.skip("offscreen 平台无可用 GL 上下文")
    img = dlg.viewer.grabFramebuffer()  # 触发真实 paintGL
    assert not img.isNull()
    dlg.close()


def test_viewer3d_playback_controls(qapp):
    """播放/暂停与速度档：播放推进帧位，sync_frame 暂停内置播放。"""
    from PySide6.QtTest import QTest

    from app.ui.skeleton3d import Skeleton3DDialog

    dlg = Skeleton3DDialog()
    dlg.load_pose3d(_make_pose3d(30), fps=1000.0)  # 高 fps 让定时器 1ms 推进
    dlg.toggle_play()
    assert dlg._playing and dlg.btn_play.text() == "暂停"
    QTest.qWait(60)
    assert dlg.viewer._pos > 0  # 播放推进
    dlg.sync_frame(0)
    assert not dlg._playing     # 外部同步暂停内置播放
    assert dlg.viewer.current_frame_index == 0
    dlg.close()


# ---- 审核页入口与帧同步 ----


@pytest.fixture
def settings(tmp_path):
    from app.ui.settings import AppSettings

    s = AppSettings()
    s._path = tmp_path / "settings.json"
    s.storage_root = str(tmp_path / "store")
    return s


@pytest.fixture
def store(settings):
    from app.session import SessionStore

    return SessionStore(settings.storage_root)


def test_review_page_view3d_entry(qapp, settings, store, tmp_path):
    """「查看 3D」入口：无 pose3d.json 禁用；有才启用；打开后走带帧同步。"""
    from app.ui.review_page import ReviewPage
    from test_ui_smoke import _write_test_clip

    record = _write_test_clip(store)
    page = ReviewPage(settings, store)
    page.load_record(record["session_id"])
    assert not page.btn_view3d.isEnabled()
    assert "pose3d" in page.btn_view3d.toolTip()

    clip_dir = store.root / record["clip_dir"]
    (clip_dir / "pose3d.json").write_text(
        json.dumps(_make_pose3d(record["frame_count"])), encoding="utf-8")
    page._update_recon3d_button()
    assert page.btn_view3d.isEnabled()

    assert page.open_3d_viewer() is True
    viewer = page._viewer3d
    assert viewer is not None
    page._frame_pos = 2
    page._show_frame()
    assert viewer.viewer.current_frame_index == 2  # 走带 → 3D 跟随
    page.step(1)
    assert viewer.viewer.current_frame_index == 3
    page.shutdown()
    assert page._viewer3d is None  # shutdown 关闭查看器
