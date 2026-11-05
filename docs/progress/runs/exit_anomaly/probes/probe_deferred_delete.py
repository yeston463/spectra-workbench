"""探针：验证 deleteLater + processEvents 是否真正销毁了 MainWindow。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra_workbench.ui.main_window import MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])
win = MainWindow()
win.show()
app.processEvents()

import weakref  # noqa: E402

ref = weakref.ref(win)
win.close()
win.deleteLater()
app.processEvents()
print("after close+deleteLater+processEvents: alive =", ref() is not None)

# 用带 DeferredDelete 的事件循环再处理一次
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
print("after sendPostedEvents(DeferredDelete): alive =", ref() is not None)
