"""UI 字体加载与环境适配工具。

确保在 Windows 桌面以及离屏（offscreen / CI）测试环境下均有可用中英文字体，
避免文字渲染为方框或字形缺失。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


def ensure_application_fonts(app: QApplication | None = None) -> int:
    """检测并配置应用程序字体。

    特别是在 offscreen 平台插件下，Qt 默认不会自动初始化 Windows 字体列表，
    本函数会主动注册 Windows 常用系统字体（如微软雅黑、Segoe UI、Arial 等）。

    返回当前字体库中的可用字体族总数。
    """
    count = len(QFontDatabase.families())
    if count > 0:
        return count

    # 常见 Windows 系统字体路径优先级
    candidate_paths = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),  # 微软雅黑
        Path(r"C:\Windows\Fonts\msyh.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),  # Segoe UI
        Path(r"C:\Windows\Fonts\arial.ttf"),  # Arial
        Path(r"C:\Windows\Fonts\simsun.ttc"),  # 宋体
    ]

    loaded_family: str | None = None
    for p in candidate_paths:
        if p.exists():
            fid = QFontDatabase.addApplicationFont(str(p))
            if fid >= 0:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams and loaded_family is None:
                    loaded_family = fams[0]

    current_app = app or QApplication.instance()
    if isinstance(current_app, QApplication) and loaded_family:
        current_app.setFont(QFont(loaded_family, 9))

    return len(QFontDatabase.families())
