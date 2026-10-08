"""主窗口：顶部工具栏式分段控件 + QStackedWidget 三页（采集 / 审核 / 设置）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.capture import FileSource
from app.session import SessionStore
from app.ui.capture_page import CapturePage
from app.ui.controller import CaptureController
from app.ui.review_page import ReviewPage
from app.ui.settings import AppSettings
from app.ui.settings_page import SettingsPage
from app.ui.theme import ui_font
from app.ui.vibrancy import apply_vibrancy

_PAGES = [("采集",), ("审核",), ("设置",)]


class MainWindow(QMainWindow):
    """batana-tool 主窗口：macOS 原生风格，分段控件切换三页。"""

    def __init__(
        self,
        settings: AppSettings,
        store: SessionStore | None = None,
        source_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store or SessionStore(Path(settings.storage_root))
        self.setWindowTitle("batana-tool — 素材采集与标注")
        self.resize(1280, 800)
        self.setMinimumSize(1100, 680)  # 最小可用尺寸：小窗口下不重叠不裁剪
        self.setUnifiedTitleAndToolBarOnMac(True)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # 工具栏：左侧产品名，中间分段控件（仿 NSToolbar 居中），垂直居中一线
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(16, 8, 16, 8)
        title = QLabel("batana-tool")
        title.setFont(ui_font(14, bold=True))
        subtitle = QLabel("素材采集与标注")
        subtitle.setObjectName("dim")
        subtitle.setFont(ui_font(12))
        top_layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignVCenter)
        top_layout.addStretch(1)

        segmented = QWidget()
        segmented.setObjectName("segmentedTrack")
        seg_layout = QHBoxLayout(segmented)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(0)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tabs: list[QPushButton] = []
        for i, (name,) in enumerate(_PAGES):
            btn = QPushButton(name)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setFont(ui_font(13))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, idx=i: self.stack.setCurrentIndex(idx))
            self._tab_group.addButton(btn, i)
            self._tabs.append(btn)
            seg_layout.addWidget(btn)
        self._tabs[0].setChecked(True)
        top_layout.addWidget(segmented, alignment=Qt.AlignmentFlag.AlignVCenter)
        top_layout.addStretch(1)
        root.addWidget(top)

        # 三页
        self.stack = QStackedWidget()
        controller = None
        if source_path:
            controller = CaptureController(
                settings, self.store, source=FileSource(source_path, loop=True)
            )
        self.capture_page = CapturePage(settings, self.store, controller=controller)
        self.review_page = ReviewPage(settings, self.store)
        self.settings_page = SettingsPage(settings)
        self.stack.addWidget(self.capture_page)
        self.stack.addWidget(self.review_page)
        self.stack.addWidget(self.settings_page)
        root.addWidget(self.stack, stretch=1)

        # 真毛玻璃：主窗口背景用 underWindowBackground 材质（NSVisualEffectView），
        # 页面留白处透出模糊桌面；失败时（offscreen/无 PyObjC）回退默认窗口底色
        apply_vibrancy(self, material="underWindowBackground")

        # H5：启动时静默重建索引，补登到素材时状态栏提示
        recovered = self.store.rebuild()
        if recovered > 0:
            self.capture_page.status_line.setText(f"已恢复 {recovered} 段素材（索引重建）")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 切到审核页时刷新列表（采集页保存的段即时可见）
        self.review_page.refresh_list()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.capture_page.shutdown()
        self.review_page.shutdown()
        super().closeEvent(event)
