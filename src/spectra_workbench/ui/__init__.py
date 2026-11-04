"""Spectra Workbench UI package."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from spectra_workbench.ui.fonts import ensure_application_fonts

# 确保在任意模式（含离屏 offscreen/CI）下初始化字体，防止字符渲染为方框
_orig_qapp_init = QApplication.__init__


def _patched_qapp_init(self: QApplication, *args: Any, **kwargs: Any) -> None:
    _orig_qapp_init(self, *args, **kwargs)
    ensure_application_fonts(self)


QApplication.__init__ = _patched_qapp_init  # type: ignore[method-assign]

_inst = QApplication.instance()
if isinstance(_inst, QApplication) and len(QFontDatabase.families()) == 0:
    ensure_application_fonts(_inst)
