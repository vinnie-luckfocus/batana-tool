"""3D 骨架查看器的相机数学与数据转换：纯 numpy，无 Qt 依赖，可无头单测。

约定：
- 世界系为显示系（y 朝上、y=0 为地面），由左目相机系（x 右 / y 下 / z 前）
  经 pose3d_to_display 转换而来；
- OrbitCamera：绕 target 的球坐标轨道相机（yaw/pitch/distance），
  视图矩阵 lookAt + 透视投影均为手写 4x4（列主序由 GL 端 glLoadMatrixf 转置处理）。
"""

from __future__ import annotations

import numpy as np

# BlazePose 33 点骨架连线表（在 recon3d.BONES 12 段基础上补全拓扑：
# 脸部从简——鼻-眼-耳-嘴轮廓；手臂到腕并补手掌三角；腿到脚跟/脚尖）
SKELETON3D_BONES: list[tuple[int, int]] = [
    # 脸部（从简）
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10),
    # 肩-髋躯干
    (11, 12), (11, 23), (12, 24), (23, 24),
    # 手臂到腕 + 手掌
    (11, 13), (13, 15), (12, 14), (14, 16),
    (15, 17), (15, 19), (15, 21), (17, 19),
    (16, 18), (16, 20), (16, 22), (18, 20),
    # 腿：髋-膝-踝-脚跟-脚尖
    (23, 25), (25, 27), (27, 29), (29, 31), (27, 31),
    (24, 26), (26, 28), (28, 30), (30, 32), (28, 32),
]

# 脚部关键点（地面高度估计用：脚踝/脚跟/脚尖）
_FOOT_INDICES = (27, 28, 29, 30, 31, 32)


def pose3d_frames_to_arrays(pose3d: dict) -> tuple[list[int], np.ndarray]:
    """pose3d dict → (frame_index 列表, points (T, 33, 3) 米)。

    掩蔽关节（坐标 null）转为 NaN，绘制层按 NaN 跳过。
    """
    indices: list[int] = []
    rows: list[np.ndarray] = []
    for f in pose3d.get("frames") or []:
        joints = f.get("joints") or []
        pts = np.full((len(joints), 3), np.nan, dtype=np.float64)
        for i, j in enumerate(joints):
            if j.get("x") is not None and j.get("z") is not None:
                pts[i] = (float(j["x"]), float(j["y"]), float(j["z"]))
        indices.append(int(f.get("frame_index", len(indices))))
        rows.append(pts)
    pts_all = np.stack(rows) if rows else np.zeros((0, 33, 3))
    return indices, pts_all


def pose3d_to_display(pts: np.ndarray, ground_y: float = 0.0) -> np.ndarray:
    """左目相机系（y 下）→ 显示系（y 上）：y_disp = ground_y - y_cam。

    ground_y 为左目系中的地面高度（脚踝附近的最大 y），使站立时脚踩 y=0。
    """
    out = np.asarray(pts, dtype=np.float64).copy()
    out[..., 1] = ground_y - out[..., 1]
    return out


def estimate_ground_y(pts_frames: np.ndarray) -> float:
    """从首若干帧的脚部关键点估计左目系地面高度（取脚部 y 的中位数上限）。

    无有效脚部点时退化为全部点的最大 y（画面最低点近似地面）。
    """
    if pts_frames.size == 0:
        return 0.0
    head = pts_frames[: min(10, len(pts_frames))]
    valid = np.isfinite(head).all(axis=-1)          # (T, 33) 完整有效点
    foot = head[:, _FOOT_INDICES, 1][valid[:, _FOOT_INDICES]]  # 有效脚部 y
    if foot.size:
        return float(np.median(foot))
    ys = head[..., 1][valid]
    return float(np.max(ys)) if ys.size else 0.0


def perspective(fov_y_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    """透视投影矩阵（行主序存储，用法见 project_points / GL 端转置加载）。"""
    f = 1.0 / np.tan(np.deg2rad(fov_y_deg) / 2.0)
    m = np.zeros((4, 4))
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


class OrbitCamera:
    """轨道相机：yaw（绕 y）/ pitch（绕 x）/ distance / target。

    默认位于骨架前方（+z 方向回看原点）：左目系 z 朝前（人正对相机），
    显示系保持 z 不变，相机在 +z 处看向 target。
    """

    MIN_DISTANCE = 0.3
    MAX_DISTANCE = 50.0
    MAX_PITCH_DEG = 89.0

    def __init__(
        self,
        target: tuple[float, float, float] = (0.0, 0.8, 0.0),
        distance: float = 3.0,
        yaw_deg: float = 0.0,
        pitch_deg: float = 10.0,
        fov_y_deg: float = 40.0,
    ) -> None:
        self.target = np.asarray(target, dtype=np.float64)
        self.distance = float(np.clip(distance, self.MIN_DISTANCE, self.MAX_DISTANCE))
        self.yaw_deg = float(yaw_deg)
        self.pitch_deg = float(np.clip(pitch_deg, -self.MAX_PITCH_DEG, self.MAX_PITCH_DEG))
        self.fov_y_deg = float(fov_y_deg)

    # ---- 交互 ----

    def orbit(self, dx_deg: float, dy_deg: float) -> None:
        """拖动旋转：dx 偏航、dy 俯仰（俯仰钳制防翻转）。"""
        self.yaw_deg = (self.yaw_deg + dx_deg) % 360.0
        self.pitch_deg = float(
            np.clip(self.pitch_deg + dy_deg, -self.MAX_PITCH_DEG, self.MAX_PITCH_DEG)
        )

    def zoom(self, factor: float) -> None:
        """滚轮缩放：factor<1 拉近；距离钳制在 [MIN, MAX]。"""
        self.distance = float(
            np.clip(self.distance * factor, self.MIN_DISTANCE, self.MAX_DISTANCE)
        )

    def pan(self, dx_world: float, dy_world: float) -> None:
        """平移 target（世界系，调用方负责把屏幕像素换算成世界量）。"""
        right = np.array([np.cos(np.deg2rad(self.yaw_deg)), 0.0,
                          np.sin(np.deg2rad(self.yaw_deg))])
        up = self._up()
        self.target = self.target + right * dx_world + up * dy_world

    # ---- 矩阵 ----

    def eye(self) -> np.ndarray:
        """相机位置（世界系）。默认（yaw=0）位于 target 的 +z 侧回看：
        与左目成像同向（x 右、人从 +z 正对相机），画面不镜像。"""
        yaw = np.deg2rad(self.yaw_deg)
        pitch = np.deg2rad(self.pitch_deg)
        offset = np.array([
            np.cos(pitch) * np.sin(yaw),
            np.sin(pitch),
            np.cos(pitch) * np.cos(yaw),
        ]) * self.distance
        return self.target + offset

    def _forward(self) -> np.ndarray:
        f = self.target - self.eye()
        return f / np.linalg.norm(f)

    def _up(self) -> np.ndarray:
        f = self._forward()
        r = np.cross(f, np.array([0.0, 1.0, 0.0]))
        r /= np.linalg.norm(r)
        return np.cross(r, f)

    def view_matrix(self) -> np.ndarray:
        """lookAt 视图矩阵（世界 → 相机系，相机 -z 朝前）。"""
        eye = self.eye()
        f = self._forward()
        r = np.cross(f, np.array([0.0, 1.0, 0.0]))
        r /= np.linalg.norm(r)
        u = np.cross(r, f)
        m = np.eye(4)
        m[0, :3], m[1, :3], m[2, :3] = r, u, -f
        m[0, 3], m[1, 3], m[2, 3] = -r @ eye, -u @ eye, f @ eye
        return m

    def projection_matrix(self, aspect: float) -> np.ndarray:
        return perspective(self.fov_y_deg, max(aspect, 1e-6), 0.05, 200.0)

    def fit(self, points: np.ndarray, aspect: float = 1.0) -> None:
        """自动取景：target 取包围盒中心，距离按包围球半径与 fov 推出。"""
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        pts = pts[np.isfinite(pts).all(axis=1)]
        if len(pts) == 0:
            return
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        center = (lo + hi) / 2.0
        radius = float(np.linalg.norm(pts - center, axis=1).max())
        radius = max(radius, 0.3)
        self.target = center
        # 距离 = 半径 / tan(fov/2)，留 1.4 倍余量
        dist = radius / np.tan(np.deg2rad(self.fov_y_deg) / 2.0) * 1.4
        self.distance = float(np.clip(dist, self.MIN_DISTANCE, self.MAX_DISTANCE))


def project_points(
    pts: np.ndarray,
    view: np.ndarray,
    proj: np.ndarray,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    """世界点 → 屏幕像素坐标。返回 ((N,2) 像素坐标, (N,) 相机系深度 z)。

    深度 <=0（相机身后）的点坐标无意义（调用方按深度过滤）。NaN 点原样传播。
    """
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
    finite = np.isfinite(pts).all(axis=1)
    hom = np.full((len(pts), 4), np.nan)
    hom[finite] = np.hstack([pts[finite], np.ones((int(finite.sum()), 1))])
    cam = hom @ view.T
    clip = cam @ proj.T
    depth = cam[:, 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        ndc = clip[:, :2] / clip[:, 3:4]
    px = np.full((len(pts), 2), np.nan)
    px[:, 0] = (ndc[:, 0] + 1.0) * 0.5 * width
    px[:, 1] = (1.0 - ndc[:, 1]) * 0.5 * height  # GL ndc y 上 → 屏幕 y 下
    return px, depth
