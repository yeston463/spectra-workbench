"""任务进度、日志监控与取消面板（P1-04 / P3-01）。

特性：
1. 任务生命周期追踪与动态状态徽标（等待、运行中、已完成、失败、已取消）。
2. 进度条联动（比例 + 阶段文本）。
3. 实时多级别彩色日志流监控。
4. 协作取消操作触发。
5. 任务成果快速加载按钮触发。
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from spectra_workbench.domain.models import TaskResult, TaskSpec


class TaskPanel(QWidget):
    """任务监控面板。"""

    sig_cancel_requested = Signal(str)  # task_id
    sig_load_result_requested = Signal(object)  # TaskResult

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_task_id: str | None = None
        self._last_result: TaskResult | None = None

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 1. 顶部状态与操作栏
        top_row = QHBoxLayout()

        self.lbl_task_info = QLabel("当前无正在运行的任务")
        self.lbl_task_info.setStyleSheet("font-weight: bold; color: #333;")
        top_row.addWidget(self.lbl_task_info, stretch=1)

        self.btn_cancel = QPushButton("🛑 取消任务")
        self.btn_cancel.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; font-weight: bold; padding: 4px 10px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #c0392b; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        top_row.addWidget(self.btn_cancel)

        self.btn_load_result = QPushButton("📂 加载成果图层")
        self.btn_load_result.setStyleSheet(
            "QPushButton { background-color: #2980b9; color: white; font-weight: bold; padding: 4px 10px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3498db; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self.btn_load_result.setEnabled(False)
        self.btn_load_result.clicked.connect(self._on_load_result_clicked)
        top_row.addWidget(self.btn_load_result)

        self.btn_clear_logs = QPushButton("清空日志")
        self.btn_clear_logs.clicked.connect(self.clear_logs)
        top_row.addWidget(self.btn_clear_logs)

        layout.addLayout(top_row)

        # 2. 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("空闲 (0%)")
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #ccc; border-radius: 3px; text-align: center; height: 18px; }"
            "QProgressBar::chunk { background-color: #3498db; }"
        )
        layout.addWidget(self.progress_bar)

        # 3. 实时日志区域
        self.txt_logs = QPlainTextEdit()
        self.txt_logs.setReadOnly(True)
        self.txt_logs.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4; font-family: Consolas, monospace; font-size: 11px;"
        )
        layout.addWidget(self.txt_logs, stretch=1)

    def on_task_started(self, spec: TaskSpec) -> None:
        """任务开始执行初始化。"""
        self._current_task_id = spec.task_id
        self._last_result = None
        self.lbl_task_info.setText(f"执行中: {spec.capability_id} [{spec.task_id}]")
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("准备中 (0%)...")
        self.btn_cancel.setEnabled(True)
        self.btn_load_result.setEnabled(False)

        self.append_log("INFO", f"任务已提交: {spec.capability_id} (ID: {spec.task_id})", "scheduler")

    def on_progress(self, ratio: float, phase: str, message: str) -> None:
        """接收进度更新。"""
        pct = max(0, min(int(ratio * 100), 100)) if ratio >= 0 else 0
        self.progress_bar.setValue(pct)
        phase_text = f" [{phase}]" if phase else ""
        msg_text = f": {message}" if message else ""
        self.progress_bar.setFormat(f"{pct}%{phase_text}{msg_text}")

        if message:
            self.append_log("INFO", f"进度 {pct}%: {phase} - {message}", "engine")

    def on_log(self, level: str, message: str, stream: str) -> None:
        """接收日志行。"""
        self.append_log(level, message, stream)

    def on_task_completed(self, result: TaskResult) -> None:
        """任务终态回调。"""
        self._last_result = result
        self.btn_cancel.setEnabled(False)

        status_val = result.status.value
        dur = result.duration_seconds
        dur_str = f"{dur:.2f}s" if dur is not None else "-"

        if status_val == "succeeded":
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat(f"已完成 (耗时 {dur_str})")
            self.progress_bar.setStyleSheet(
                "QProgressBar { border: 1px solid #ccc; border-radius: 3px; text-align: center; height: 18px; }"
                "QProgressBar::chunk { background-color: #27ae60; }"
            )
            self.lbl_task_info.setText(f"✅ 任务成功完成: {result.task_id} (耗时 {dur_str})")
            self.btn_load_result.setEnabled(bool(result.outputs))
            self.append_log("INFO", f"任务成功完成: {result.task_id}，产生 {len(result.outputs)} 项成果", "scheduler")
        elif status_val == "cancelled":
            self.progress_bar.setFormat("任务已取消")
            self.progress_bar.setStyleSheet(
                "QProgressBar { border: 1px solid #ccc; border-radius: 3px; text-align: center; height: 18px; }"
                "QProgressBar::chunk { background-color: #f39c12; }"
            )
            self.lbl_task_info.setText(f"⚠️ 任务已取消: {result.task_id}")
            self.append_log("WARNING", f"任务已被用户取消: {result.task_id}", "scheduler")
        else:
            self.progress_bar.setFormat("任务执行失败")
            self.progress_bar.setStyleSheet(
                "QProgressBar { border: 1px solid #ccc; border-radius: 3px; text-align: center; height: 18px; }"
                "QProgressBar::chunk { background-color: #e74c3c; }"
            )
            err_msg = result.error.get("message") if result.error else "未知错误"
            self.lbl_task_info.setText(f"❌ 任务失败: {result.task_id} - {err_msg}")
            self.append_log("ERROR", f"任务失败: {result.error}", "scheduler")

    def on_task_error(self, code: str, message: str) -> None:
        """任务提交或通信错误。"""
        self.btn_cancel.setEnabled(False)
        self.lbl_task_info.setText(f"❌ 错误: {code}")
        self.append_log("ERROR", f"[{code}] {message}", "scheduler")

    def append_log(self, level: str, message: str, stream: str = "engine") -> None:
        """彩色追加单行日志。"""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        cursor = self.txt_logs.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)

        fmt = QTextCharFormat()
        lvl = level.upper()
        if lvl == "ERROR":
            fmt.setForeground(QColor("#ff6b6b"))
        elif lvl == "WARNING":
            fmt.setForeground(QColor("#feca57"))
        elif lvl == "INFO":
            fmt.setForeground(QColor("#48dbfb"))
        else:
            fmt.setForeground(QColor("#a4b0be"))

        cursor.insertText(f"[{now}] [{lvl}] [{stream}] {message}\n", fmt)
        self.txt_logs.setTextCursor(cursor)
        self.txt_logs.ensureCursorVisible()

    def clear_logs(self) -> None:
        self.txt_logs.clear()

    def _on_cancel_clicked(self) -> None:
        if self._current_task_id:
            self.btn_cancel.setEnabled(False)
            self.append_log("WARNING", f"正在请求协作取消任务: {self._current_task_id}...", "scheduler")
            self.sig_cancel_requested.emit(self._current_task_id)

    def _on_load_result_clicked(self) -> None:
        if self._last_result:
            self.sig_load_result_requested.emit(self._last_result)
