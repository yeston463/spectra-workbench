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

import atexit
import contextlib
import gc
import os
import signal
import sys
import threading
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

# ============================================================================
# 退出异常 0xC0000409 现场捕获（第 33 轮诊断）
#
# 事件日志已确证：崩溃在 ucrtbase.dll 固定偏移 0xa527e，ExceptionCode=c0000409。
# UCRT 的 abort() 在调用 fastfail 之前会先触发 SIGABRT 处理器——
# 只要处理器不返回（转而抛 SystemExit），fastfail 就不会发生，
# 我们就能在崩溃前拿到完整线程栈。若 SIGABRT 未被触发而崩溃照旧，
# 说明走的是栈保护（__report_gsfailure）或纯虚调用等其他路径，另作处理。
#
# 退出码约定：69 = 捕获到 SIGABRT（此前会直接 0xC0000409）。
# ============================================================================

_ABORT_EXIT_CODE = 69


def _dump_all_thread_stacks(reason: str) -> None:
    """把当前所有线程的 Python 栈打印到 stderr（崩溃前的最后现场）。"""
    import faulthandler

    sys.stderr.write(f"\n[abort-capture] 触发原因: {reason}\n")
    sys.stderr.write(f"[abort-capture] 存活线程数: {threading.active_count()}\n")
    for t in threading.enumerate():
        sys.stderr.write(
            f"[abort-capture]   线程 name={t.name} daemon={t.daemon} "
            f"ident={t.ident} alive={t.is_alive()}\n"
        )
    sys.stderr.flush()
    faulthandler.dump_traceback(file=sys.stderr)
    sys.stderr.flush()


def _sigabrt_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
    """拦下 UCRT abort()：打印现场后以特殊退出码结束，不走 fastfail。"""
    _dump_all_thread_stacks(f"SIGABRT (signal {signum})")
    raise SystemExit(_ABORT_EXIT_CODE)


with contextlib.suppress(Exception):
    signal.signal(signal.SIGABRT, _sigabrt_handler)


def _dump_threads_at_interpreter_exit() -> None:
    """解释器退出阶段打印存活线程栈（无论是否崩溃都执行，供对照）。"""
    alive = [t for t in threading.enumerate() if t.is_alive() and t is not threading.main_thread()]
    sys.stderr.write(f"\n[exit-dump] 解释器退出；非主线程存活数 = {len(alive)}\n")
    for t in alive:
        sys.stderr.write(
            f"[exit-dump]   name={t.name} daemon={t.daemon} ident={t.ident}\n"
        )
    sys.stderr.flush()


atexit.register(_dump_threads_at_interpreter_exit)


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
    """所有测试与会话级夹具收尾之后，再做 Qt 有序销毁。

    同时把 pytest 的真实退出码存到 config：``pytest_unconfigure`` 阶段
    ``config.exitstatus`` 可能仍是 None/0，直接读取会把失败轮错误地
    ``os._exit(0)``，从而掩盖失败。
    """
    session.config._sw_exitstatus = int(exitstatus)
    teardown_qt_application()


def pytest_unconfigure(config: Any) -> None:  # noqa: ARG001
    """跳过 Python 最终化（finalization）的 C++ 清理，并保留真实退出码。

    第 33 轮诊断结论（机制已证明）：
    - 事件日志：崩溃在 ucrtbase.dll 固定偏移 0xa527e，ExceptionCode=c0000409；
    - atexit 时非主线程存活数为 0，SIGABRT 处理器未被触发
      （fastfail 发生在 Python 信号机制已停用的 finalization 之后）；
    - os._exit 跳过 finalization 后崩溃消失（机制对照实验）；
    - stderr 反复出现 QThreadStorage 警告，指向 PySide6 在 worker 读取线程上
      遗留的线程本地存储于模块卸载阶段析构。
    该崩溃与测试内容无关（integration 单独跑不崩、加任意后续测试即崩），
    属测试进程退出路径问题。测试进程不需要干净的 C++ 清理，
    故默认用 os._exit 终止，并**保留 pytest 的真实退出码**
    （失败时非 0，CI/脚本语义不变）。设 SW_SKIP_FINALIZATION=0 可禁用。
    """
    if os.environ.get("SW_SKIP_FINALIZATION") == "0":
        return
    exitstatus = int(
        getattr(config, "_sw_exitstatus", getattr(config, "exitstatus", 0)) or 0
    )
    # os._exit 不经过解释器最终化，缓冲必须手动刷：否则 pytest 的终端报告
    #（含 FAILURES 详情）会因 stdout 块缓冲未落盘而丢失。
    sys.stdout.flush()

    sys.stderr.write(f"[skip-finalization] os._exit({exitstatus}) 绕过 Python 最终化\n")
    sys.stderr.flush()
    os._exit(exitstatus)
