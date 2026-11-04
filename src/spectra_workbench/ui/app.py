"""桌面应用程序启动入口（P3-01）。

支持：
1. 交互式启动：python -m spectra_workbench.ui.app
2. 自动化启动与样例加载：python -m spectra_workbench.ui.app --sample
3. 自动化窗口截图导出：python -m spectra_workbench.ui.app --sample --screenshot screenshot.png
4. 打开指定影像：python -m spectra_workbench.ui.app --open path/to/image.tif
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from spectra_workbench.ui.main_window import MainWindow


def run_app(argv: list[str] | None = None) -> int:
    """应用程序入口函数。"""
    parser = argparse.ArgumentParser(description="Spectra Workbench 桌面工作台")
    parser.add_argument("--open", dest="open_path", type=str, default=None, help="启动时自动打开的影像文件路径")
    parser.add_argument("--sample", action="store_true", help="启动时自动加载预置 A01 合成多光谱影像样例")
    parser.add_argument("--screenshot", dest="screenshot_path", type=str, default=None, help="渲染就绪后保存窗口截图并退出")
    parser.add_argument("--headless", action="store_true", help="无头模式运行 (QT_QPA_PLATFORM=offscreen)")
    parser.add_argument("--project-root", dest="project_root", type=str, default=None, help="项目根目录")

    args = parser.parse_args(argv or sys.argv[1:])

    if args.headless or (args.screenshot_path and os.environ.get("QT_QPA_PLATFORM") == "offscreen"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])

    app.setApplicationName("SpectraWorkbench")
    app.setOrganizationName("SpectraWorkbench")

    root = Path(args.project_root) if args.project_root else Path.cwd()
    window = MainWindow(project_root=root)
    window.show()

    # 打开指定文件或样例
    if args.open_path:
        QTimer.singleShot(100, lambda: window.open_dataset(args.open_path))
    elif args.sample:
        QTimer.singleShot(100, window.load_default_sample)

    # 截图与自动退出支持
    if args.screenshot_path:
        out_shot = Path(args.screenshot_path).resolve()
        out_shot.parent.mkdir(parents=True, exist_ok=True)

        def _capture() -> None:
            # 确保事件循环已充分处理绘制
            app.processEvents()
            pixmap = window.grab()
            pixmap.save(str(out_shot))
            print(f">>> 窗口截图已成功保存至: {out_shot}")
            QTimer.singleShot(200, app.quit)

        # 等待分块读取与渲染完成（延时 1500ms）
        QTimer.singleShot(1800, _capture)

    return app.exec()


if __name__ == "__main__":
    sys.exit(run_app())
