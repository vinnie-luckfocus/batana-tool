"""环境体检对话框（PRD F11 UI）：进度条 + 逐项结果表 + 大号 overall 结论。

检查在 QThread（_EnvCheckWorker）中执行，UI 不卡；结果表状态列用系统语义色：
pass 绿 / warn 橙 / fail 红 / skip 灰。整体设计遵循 macOS 原生设计系统。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.envcheck import EnvironmentReport, EnvironmentChecker
from app.envcheck.models import (
    OVERALL_FAIL,
    OVERALL_OK,
    OVERALL_WARN,
    STATUS_FAIL,
    STATUS_LABELS,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
)
from app.ui.theme import COLORS, semantic_hex, title_font, ui_font

# 状态列底色（系统语义色；显式底色在浅/深外观下均可读）
_STATUS_COLORS = {
    STATUS_PASS: COLORS["green"],
    STATUS_WARN: COLORS["orange"],
    STATUS_FAIL: COLORS["red"],
    STATUS_SKIP: COLORS["fg_dim"],
}

_OVERALL_COLORS = {
    OVERALL_OK: "green",
    OVERALL_WARN: "orange",
    OVERALL_FAIL: "red",
}


class _EnvCheckWorker(QThread):
    """体检线程：跑 EnvironmentChecker.run，进度/结果走 Qt 信号回主线程。"""

    progressed = Signal(int, int, str)      # (已完成, 总项数, 当前检查中文名)
    finished_report = Signal(object)        # EnvironmentReport
    failed = Signal(str)

    def __init__(self, checker: EnvironmentChecker, duration_s: float, parent=None) -> None:
        super().__init__(parent)
        self._checker = checker
        self._duration_s = duration_s

    def run(self) -> None:
        try:
            report = self._checker.run(
                duration_s=self._duration_s,
                progress_cb=lambda d, t, n: self.progressed.emit(d, t, n),
            )
            self.finished_report.emit(report)
        except Exception as e:  # 相机断开/文件损坏等
            self.failed.emit(str(e))


class EnvCheckDialog(QDialog):
    """环境体检对话框：构造后调用 start_check() 开始。"""

    def __init__(
        self,
        checker: EnvironmentChecker,
        duration_s: float = 10.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.checker = checker
        self.duration_s = duration_s
        self._worker: _EnvCheckWorker | None = None
        self.report: EnvironmentReport | None = None
        self.setWindowTitle("环境体检")
        self.resize(760, 520)
        self._build_ui()

    # ---- UI 组装 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("环境体检")
        title.setFont(title_font(17))
        top_layout.addWidget(title)
        self.label_hint = QLabel(f"采样 {self.duration_s:.0f} 秒，请保持采集位实景")
        self.label_hint.setObjectName("dim")
        self.label_hint.setFont(ui_font(12))
        top_layout.addSpacing(10)
        top_layout.addWidget(self.label_hint)
        top_layout.addStretch(1)
        root.addWidget(top)

        self.progress = QProgressBar()
        self.progress.setRange(0, 8)
        self.progress.setValue(0)
        self.progress.setFormat("%v / 8")
        root.addWidget(self.progress)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["状态", "检查项", "实测值", "建议"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, stretch=1)

        # 底部：overall 大号结论 + 操作按钮
        bottom = QWidget()
        bt = QHBoxLayout(bottom)
        bt.setContentsMargins(0, 0, 0, 0)
        self.label_overall = QLabel("--")
        self.label_overall.setFont(title_font(24))
        self.label_overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bt.addWidget(self.label_overall, stretch=1)
        self.btn_start = QPushButton("开始检查")
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self.start_check)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        bt.addWidget(self.btn_start)
        bt.addWidget(self.btn_close)
        root.addWidget(bottom)

    # ---- 检查流程 ----

    def start_check(self) -> None:
        """开始体检（QThread 后台执行）。"""
        if self._worker is not None and self._worker.isRunning():
            return
        self.btn_start.setEnabled(False)
        self.table.setRowCount(0)
        self.label_overall.setText("检查中…")
        self.label_overall.setStyleSheet(f"color: {semantic_hex('fg_dim')};")
        self.progress.setValue(0)
        self._worker = _EnvCheckWorker(self.checker, self.duration_s, parent=self)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished_report.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.label_hint.setText(name)

    def _on_finished(self, report: EnvironmentReport) -> None:
        self.report = report
        self.show_report(report)
        self.btn_start.setEnabled(True)
        self.btn_start.setText("重新检查")
        self.label_hint.setText(
            f"报告已保存 {report.report_path}" if report.report_path else "检查完成"
        )

    def _on_failed(self, message: str) -> None:
        self.label_overall.setText("检查失败")
        self.label_overall.setStyleSheet(f"color: {semantic_hex('red')};")
        self.label_hint.setText(f"错误：{message}")
        self.btn_start.setEnabled(True)

    # ---- 报告渲染（测试可直接调用） ----

    def show_report(self, report: EnvironmentReport) -> None:
        """把报告渲染进结果表与 overall 大号结论。"""
        self.table.setRowCount(len(report.results))
        for row, r in enumerate(report.results):
            status_item = QTableWidgetItem(STATUS_LABELS[r.status])
            status_item.setBackground(QColor(_STATUS_COLORS[r.status]))
            status_item.setForeground(QColor("#FFFFFF"))
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, status_item)
            self.table.setItem(row, 1, QTableWidgetItem(r.name))
            self.table.setItem(row, 2, QTableWidgetItem(r.measured))
            self.table.setItem(row, 3, QTableWidgetItem(r.suggestion))
        overall = report.overall
        self.label_overall.setText(overall)
        self.label_overall.setStyleSheet(f"color: {semantic_hex(_OVERALL_COLORS[overall])};")

    # ---- 关闭 ----

    def reject(self) -> None:  # noqa: N802（Qt 命名）
        self.checker.cancel()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().reject()
