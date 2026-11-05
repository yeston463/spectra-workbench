"""共享测试夹具：会话结束时按序销毁 Qt 对象。

## 背景（退出异常诊断结论）

全量测试曾偶发在「pytest 已经报告 passed 之后」以退出码
0xC0000409（STATUS_STACK_BUFFER_OVERRUN）结束。Windows 应用程序事件日志给出的
崩溃现场是：

    出错模块名称：ucrtbase.dll   异常代码：0xc0000409
    出错模块名称：Qt6Core.dll     异常代码：0xc0000409

`ucrtbase.dll` 中该偏移是一条 `int 0x15`（`__fastfail`）指令，即 CRT 在
检测到栈状态异常后立即终止进程；而 `Qt6Core.dll` 的那一次说明故障点在 Qt 自身。

两类现场都发生在**解释器退出阶段**：此时 QApplication 单例与其下属
QWidget / QObject 的析构顺序不确定，一旦某个 QWidget 在 QApplication 之后
才被销毁，就会命中未定义行为。测试里普遍使用的
`win.deleteLater()` + `qapp.processEvents()` 组合并不可靠——`processEvents()`
默认不处理 DeferredDelete 事件，窗口往往要等到解释器收尾才被回收，
于是大量窗口与其持有的 worker 客户端一起堆到退出阶段集中析构。

## 对策

在解释器退出之前，于 `pytest_sessionfinish` 中显式、有序地完成 Qt 收尾：
关闭并销毁全部顶层控件 → 派发 DeferredDelete → 回收 Python 引用 →
退出 QApplication。这样 Qt 对象的析构发生在解释器收尾之前，顺序确定。

这不是「屏蔽清理」或「忽略退出码」：所有清理动作都真实执行，
最终进程的退出码仍由 pytest 自身产生并透传。
"""

from __future__ import annotations

import contextlib
import gc
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


def _drain_qt_events(app: QApplication, rounds: int = 3) -> None:
    """派发 DeferredDelete 并处理事件，直到没有待处理的删除。"""
    for _ in range(rounds):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()


def teardown_qt_application() -> None:
    """有序销毁 Qt 对象，避开解释器退出阶段的不确定析构顺序。"""
    app = QApplication.instance()
    if app is None:
        return

    # 1) 关闭全部顶层控件（触发各自的 closeEvent，释放 worker 客户端等资源）
    for widget in list(app.topLevelWidgets()):
        with contextlib.suppress(Exception):
            widget.close()
    _drain_qt_events(app)

    # 2) 回收 Python 侧引用；PySide6 在最后一个 Python 包装对象消失时销毁 C++ 对象
    gc.collect()
    _drain_qt_events(app)

    # 3) 退出 QApplication：此后不再有新的 Qt 事件投递
    with contextlib.suppress(Exception):
        app.quit()
    with contextlib.suppress(Exception):
        app.processEvents()
    gc.collect()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:  # noqa: ARG001
    """所有测试与会话级夹具收尾之后，再做 Qt 有序销毁。"""
    teardown_qt_application()
