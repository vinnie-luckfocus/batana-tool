"""主窗口：顶部页签切换 + QStackedWidget 三页（采集 / 审核 / 设置）。"""

from __future__ import annotations

from pathlib import Path

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
from app.ui.theme import COLORS, header_font, mono_font

_PAGES = [("CAPTURE", "采集"), ("REVIEW", "审核"), ("SETTINGS", "设置")]


class MainWindow(QMainWindow):
    """batana-tool 主窗口：战术遥测风格三页结构。"""

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
        self.setWindowTitle("BATANA-TOOL // 素材采集与标注")
        self.resize(1280, 800)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(1)
        self.setCentralWidget(central)

        # 顶栏：结构性大标题 + 页签（1px 网格缝）
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(12, 8, 12, 4)
        title = QLabel("BATANA-TOOL")
        title.setFont(header_font(22))
        title.setStyleSheet(f"color: {COLORS['fg']};")
        subtitle = QLabel("素材采集与标注 // SESSION RECORDER")
        subtitle.setObjectName("dim")
        subtitle.setFont(mono_font(9, letter_spacing=2.5))
        top_layout.addWidget(title)
        top_layout.addWidget(subtitle)
        top_layout.addStretch(1)

        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tabs: list[QPushButton] = []
        for i, (name, _zh) in enumerate(_PAGES):
            btn = QPushButton(f"[ {name} ]")
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.setFont(mono_font(11, bold=True, letter_spacing=2.0))
            btn.clicked.connect(lambda _c=False, idx=i: self.stack.setCurrentIndex(idx))
            self._tab_group.addButton(btn, i)
            self._tabs.append(btn)
            top_layout.addWidget(btn)
        self._tabs[0].setChecked(True)
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

        # H5：启动时静默重建索引，补登到素材时状态栏提示
        recovered = self.store.rebuild()
        if recovered > 0:
            self.capture_page.status_line.setText(f">>> 已恢复 {recovered} 段素材（索引重建）")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 切到审核页时刷新列表（采集页保存的段即时可见）
        self.review_page.refresh_list()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.capture_page.shutdown()
        self.review_page.shutdown()
        super().closeEvent(event)
