"""3D 骨架查看器：QOpenGLWidget 手写渲染（无第三方 GL 依赖）。

渲染路径：现代 GL（3.3 core profile + 着色器），兼容 macOS Qt6 默认上下文；
着色器只做 mvp 变换 + 单色输出，顶点数据每帧上传（33 点量级，开销忽略）。
相机数学与数据转换在 app/ui/skeleton3d_math.py（纯 numpy，可无头单测）。

配色沿用 2D 叠加约定：身体左侧橙 / 右侧蓝 / 中线灰（语义色，跟随系统外观）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QMatrix4x4, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.skeleton3d_math import (
    SKELETON3D_BONES,
    OrbitCamera,
    estimate_ground_y,
    pose3d_frames_to_arrays,
    pose3d_to_display,
)
from app.ui.theme import mono_font, semantic_color

# GL 常量（PySide6 不导出 GL_* 枚举，值由 OpenGL 规范固定）
GL_POINTS = 0x0000
GL_LINES = 0x0001
GL_FLOAT = 0x1406
GL_DEPTH_BUFFER_BIT = 0x0100
GL_COLOR_BUFFER_BIT = 0x4000
GL_DEPTH_TEST = 0x0B71
GL_PROGRAM_POINT_SIZE = 0x8642

# 身体侧分组（2D 叠加同款配色约定：左橙 / 右蓝 / 中线灰）
_LEFT_KP = {1, 2, 3, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31}
_RIGHT_KP = {4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32}

# 回放速度档（与审核页走带一致）
_SPEEDS = [("0.25×", 0.25), ("0.5×", 0.5), ("1×", 1.0)]

_VERT_SRC = """#version 330 core
layout(location = 0) in vec3 position;
uniform mat4 mvp;
void main() {
    gl_Position = mvp * vec4(position, 1.0);
    gl_PointSize = 6.0;
}
"""

_FRAG_SRC = """#version 330 core
uniform vec4 u_color;
out vec4 fragColor;
void main() { fragColor = u_color; }
"""


def load_pose3d(path: str | Path) -> dict:
    """读取 pose3d.json。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _gl_color(name: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    c = semantic_color(name)
    return (c.redF(), c.greenF(), c.blueF(), alpha)


class Skeleton3DWidget(QOpenGLWidget):
    """3D 骨架视口：点 + 连线 + y=0 地面网格；左键旋转 / 右键平移 / 滚轮缩放。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 固定 3.3 core：macOS Qt6 可稳定获得；须在控件显示前设置
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(24)
        self.setFormat(fmt)
        self.camera = OrbitCamera()
        self._indices: list[int] = []
        self._pos_of_index: dict[int, int] = {}
        self._frames = np.zeros((0, 33, 3))
        self._pos = 0
        self._gl_ok = False
        self._program = None
        self._vao = None
        self._vbo = None
        self._drag_pos = None
        self._drag_button = None
        self.setMinimumSize(320, 240)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # ---- 数据 ----

    def set_data(self, frame_indices: list[int], pts_display: np.ndarray) -> None:
        """载入骨架序列（显示系坐标，(T, 33, 3)，NaN = 掩蔽）并自动取景。"""
        self._indices = list(frame_indices)
        self._pos_of_index = {fi: i for i, fi in enumerate(self._indices)}
        self._frames = np.asarray(pts_display, dtype=np.float64)
        self._pos = 0
        if len(self._frames):
            first = self._frames[0]
            self.camera.fit(first[np.isfinite(first).all(axis=1)])

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def current_frame_index(self) -> int | None:
        if not self._indices:
            return None
        return self._indices[self._pos]

    def set_frame_pos(self, pos: int) -> None:
        """按序列位置定位（0..T-1）。"""
        if not self._indices:
            return
        self._pos = int(np.clip(pos, 0, len(self._indices) - 1))
        self.update()

    def sync_frame(self, frame_index: int) -> bool:
        """按视频帧号定位（外部同步入口）；该帧无 3D 数据时返回 False 不动。"""
        pos = self._pos_of_index.get(frame_index)
        if pos is None:
            return False
        self.set_frame_pos(pos)
        return True

    # ---- GL ----

    def initializeGL(self) -> None:  # noqa: N802
        # Qt6 中着色器/缓冲类均在 QtOpenGL 模块（QtGui 不再导出 QOpenGLShader）
        from PySide6.QtOpenGL import (
            QOpenGLBuffer,
            QOpenGLShader,
            QOpenGLShaderProgram,
            QOpenGLVertexArrayObject,
        )

        try:
            program = QOpenGLShaderProgram()
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERT_SRC):
                raise RuntimeError(f"顶点着色器编译失败: {program.log()}")
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _FRAG_SRC):
                raise RuntimeError(f"片元着色器编译失败: {program.log()}")
            if not program.link():
                raise RuntimeError(f"着色器链接失败: {program.log()}")
            self._program = program
            self._vao = QOpenGLVertexArrayObject()  # 不挂 Qt 父子树（GL 资源随解释器回收）
            self._vao.create()
            self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._vbo.create()
            f = self.context().functions()
            f.glEnable(GL_DEPTH_TEST)
            f.glEnable(GL_PROGRAM_POINT_SIZE)  # 着色器控制点大小（core profile 必须）
            # 上下文销毁前显式释放 GL 资源，避免解释器退出时按死亡上下文回收崩溃
            self.context().aboutToBeDestroyed.connect(self._cleanup_gl)
            self._gl_ok = True
        except Exception:
            import traceback
            # 静默吞异常会让用户面对黑屏无任何线索——必须留痕
            traceback.print_exc()
            self._gl_ok = False
            self._program = None

    def _cleanup_gl(self) -> None:
        """GL 资源随上下文销毁（QOpenGLWidget::aboutToBeDestroyed 回调）。"""
        if self._vbo is not None:
            self._vbo.destroy()
        if self._vao is not None:
            self._vao.destroy()
        self._vbo = None
        self._vao = None
        self._program = None
        self._gl_ok = False

    def resizeGL(self, w: int, h: int) -> None:  # noqa: N802
        if self.context() is not None:
            self.context().functions().glViewport(0, 0, w, h)

    def _draw_arrays(self, mode: int, verts: np.ndarray, color: tuple) -> None:
        """上传顶点并绘制（数据量极小，每帧重新上传即可）。"""
        f = self.context().functions()
        data = np.ascontiguousarray(verts, dtype=np.float32).reshape(-1, 3)
        self._vbo.bind()
        self._vbo.allocate(data.tobytes(), data.nbytes)
        self._program.setAttributeBuffer(0, GL_FLOAT, 0, 3)
        self._program.enableAttributeArray(0)
        self._program.setUniformValue("u_color", QColor.fromRgbF(*color))
        f.glDrawArrays(mode, 0, len(data))
        self._vbo.release()

    def paintGL(self) -> None:  # noqa: N802
        f = self.context().functions()
        bg = self.palette().color(self.backgroundRole())
        f.glClearColor(bg.redF(), bg.greenF(), bg.blueF(), 1.0)
        f.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if not self._gl_ok or self._program is None:
            return
        aspect = max(self.width(), 1) / max(self.height(), 1)
        mvp = self.camera.projection_matrix(aspect) @ self.camera.view_matrix()
        qm = QMatrix4x4(*np.asarray(mvp, dtype=np.float64).ravel().tolist())

        self._program.bind()
        self._vao.bind()
        self._program.setUniformValue("mvp", qm)

        # 地面网格 y=0：x/z ∈ [-3, 3]，间隔 0.5m
        grid = []
        for v in np.arange(-3.0, 3.01, 0.5):
            grid += [[v, 0.0, -3.0], [v, 0.0, 3.0], [-3.0, 0.0, v], [3.0, 0.0, v]]
        self._draw_arrays(GL_LINES, np.asarray(grid), _gl_color("fg_dim", 0.35))

        if len(self._frames):
            pts = self._frames[self._pos]
            ok = np.isfinite(pts).all(axis=1)
            # 骨架连线（两端点均有效才画；按身体侧着色）
            for side_name, group in (("orange", _LEFT_KP), ("blue", _RIGHT_KP)):
                segs = []
                for i, j in SKELETON3D_BONES:
                    if ok[i] and ok[j] and i in group and j in group:
                        segs += [pts[i], pts[j]]
                if segs:
                    self._draw_arrays(GL_LINES, np.asarray(segs), _gl_color(side_name))
            mid = []
            for i, j in SKELETON3D_BONES:
                if ok[i] and ok[j] and not ({i, j} <= _LEFT_KP or {i, j} <= _RIGHT_KP):
                    mid += [pts[i], pts[j]]
            if mid:
                self._draw_arrays(GL_LINES, np.asarray(mid), _gl_color("fg_dim"))
            # 关节点
            if ok.any():
                self._draw_arrays(GL_POINTS, pts[ok], _gl_color("fg"))

        self._vao.release()
        self._program.release()

    # ---- 交互 ----

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_pos = event.position()
        self._drag_button = event.button()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_pos is None:
            return
        pos = event.position()
        dx, dy = pos.x() - self._drag_pos.x(), pos.y() - self._drag_pos.y()
        self._drag_pos = pos
        if self._drag_button == Qt.MouseButton.LeftButton:
            self.camera.orbit(dx * 0.4, dy * 0.4)
        elif self._drag_button == Qt.MouseButton.RightButton:
            # 屏幕像素 → 世界平移量：按当前距离与 fov 换算可视高度
            scale = self.camera.distance * np.tan(np.deg2rad(self.camera.fov_y_deg) / 2) * 2 \
                / max(self.height(), 1)
            self.camera.pan(-dx * scale, dy * scale)
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_pos = None
        self._drag_button = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        self.camera.zoom(0.9 ** steps)
        self.update()
        event.accept()


class Skeleton3DDialog(QDialog):
    """3D 骨架查看对话框（非模态）：视口 + 播放/暂停 + 速度档 + 帧号。

    帧同步单向：审核页走带 → sync_frame 跟随（跟随会暂停内置播放）；
    内置播放不回写审核页，避免耦合回路。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("3D 骨架")
        self.resize(720, 560)
        self._fps = 120.0
        self._playing = False

        layout = QVBoxLayout(self)
        self.viewer = Skeleton3DWidget(self)
        layout.addWidget(self.viewer, stretch=1)

        transport = QHBoxLayout()
        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self.toggle_play)
        self.combo_speed = QComboBox()
        for name, _v in _SPEEDS:
            self.combo_speed.addItem(name)
        self.combo_speed.setCurrentIndex(2)
        self.combo_speed.currentIndexChanged.connect(lambda _i: self._restart_timer())
        self.label_frame = QLabel("帧 --/--")
        self.label_frame.setFont(mono_font(12))
        transport.addWidget(self.btn_play)
        transport.addWidget(self.combo_speed)
        transport.addStretch(1)
        transport.addWidget(self.label_frame)
        layout.addLayout(transport)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    # ---- 数据 ----

    def load_pose3d(self, data: dict, fps: float) -> None:
        """载入 pose3d dict（掩蔽关节转 NaN，左目系 y 翻转为显示系 y 朝上）。"""
        self._fps = float(fps) if fps > 0 else 120.0
        indices, pts = pose3d_frames_to_arrays(data)
        ground = estimate_ground_y(pts)
        self.viewer.set_data(indices, pose3d_to_display(pts, ground))
        self._update_label()

    def load_pose3d_file(self, path: str | Path, fps: float) -> None:
        self.load_pose3d(load_pose3d(path), fps)

    # ---- 外部同步（审核页走带 → 3D 视图） ----

    def sync_frame(self, frame_index: int) -> None:
        """跟随外部帧号；外部驱动优先，暂停内置播放。"""
        if self._playing:
            self.stop_play()
        self.viewer.sync_frame(frame_index)
        self._update_label()

    # ---- 内置播放 ----

    def toggle_play(self) -> None:
        if self._playing:
            self.stop_play()
        elif self.viewer.frame_count:
            self._playing = True
            self.btn_play.setText("暂停")
            self._restart_timer()

    def stop_play(self) -> None:
        self._playing = False
        self._timer.stop()
        self.btn_play.setText("播放")

    def _restart_timer(self) -> None:
        if self._playing:
            speed = _SPEEDS[self.combo_speed.currentIndex()][1]
            self._timer.start(max(1, int(1000 / self._fps / speed)))

    def _advance(self) -> None:
        n = self.viewer.frame_count
        if not n:
            self.stop_play()
            return
        self.viewer.set_frame_pos((self.viewer._pos + 1) % n)  # 循环播放
        self._update_label()

    def _update_label(self) -> None:
        n = self.viewer.frame_count
        cur = self.viewer.current_frame_index
        self.label_frame.setText(f"帧 {cur:04d}/{n - 1:04d}" if n and cur is not None else "帧 --/--")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_play()
        super().closeEvent(event)
