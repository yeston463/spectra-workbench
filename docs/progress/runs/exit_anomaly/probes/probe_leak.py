"""探针：统计反复创建/关闭 MainWindow 后残留的 Qt 对象与 worker 进程。"""
from __future__ import annotations

import gc
import os
import sys
import time

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra_workbench.ui.main_window import MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])

for i in range(6):
    win = MainWindow()
    win.show()
    app.processEvents()
    win.close()
    win.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    del win
    gc.collect()
    print(f"iter {i}: top-level widgets = {len(app.topLevelWidgets())}")

print("--- 强制清理 DeferredDelete ---")
for _ in range(5):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()
print("top-level widgets after cleanup =", len(app.topLevelWidgets()))
