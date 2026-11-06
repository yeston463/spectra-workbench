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

**退出策略（S1）**：本文件不再调用 `os._exit`。默认路径完整执行 pytest 收尾与
Python finalization，由外层监督器以真实操作系统退出码判定是否 Clean。
确需临时绕过 finalization 时，只能由 `scripts/pytest_runner.py --mode bypass`
在 `pytest.main()` 返回后显式执行，并在结果中标记
`bypass_finalization=true`、`clean_exit_verified=false`；Bypass 不满足发布门槛。
"""

from __future__ import annotations

import atexit
import contextlib
import gc
import json
import os
import signal
import sys
import threading
from pathlib import Path
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


# ============================================================================
# S1：把真实清理异常交给外层监督器，不再用 suppress(Exception) 静默抹去。
# ============================================================================

_cleanup_errors: list[str] = []
_cleanup_checks: list[str] = []


def _record_check(name: str) -> None:
    _cleanup_checks.append(name)


def _record_cleanup_error(stage: str, exc: BaseException) -> None:
    _cleanup_errors.append(f"{stage}: {type(exc).__name__}: {exc}")


def _write_cleanup_report(pytest_exitstatus: int) -> None:
    """把清理检查完成情况写到 supervisor 指定的路径。

    缺失报告（进程崩溃/未到 sessionfinish）会被 supervisor 视为未验证，
    不能因为「没有错误列表」就当作干净。
    """
    path_text = os.environ.get("SW_CLEANUP_REPORT")
    if not path_text:
        return
    path = Path(path_text)
    injected = os.environ.get("SW_CLEANUP_INJECT_ERROR")
    if injected:
        _cleanup_errors.append(f"injected_cleanup_error: {injected}")
    payload = {
        "schema_version": "1.0",
        "complete": True,
        "errors": list(_cleanup_errors),
        "checks": list(_cleanup_checks),
        "pytest_exitstatus": int(pytest_exitstatus),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp_{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        _cleanup_errors.append(f"write_cleanup_report: {type(exc).__name__}: {exc}")
        sys.stderr.write(f"[cleanup-report] write failed: {exc}\n")
        sys.stderr.flush()


def _drain_qt_events(app: QApplication, rounds: int = 3) -> None:
    """派发 DeferredDelete 并处理事件，直到没有待处理的删除。"""
    for _ in range(rounds):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()


def teardown_qt_application() -> None:
    """有序销毁 Qt 对象，并把每一步的真实异常记录到 cleanup report。"""
    app = QApplication.instance()
    if app is None:
        _record_check("no_qapplication")
        return

    # 1) 关闭全部顶层控件（触发各自的 closeEvent，释放 worker 客户端等资源）
    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
        except Exception as exc:
            _record_cleanup_error(f"close_widget:{type(widget).__name__}", exc)
    _record_check("close_top_levels")
    try:
        _drain_qt_events(app)
    except Exception as exc:
        _record_cleanup_error("drain_after_close", exc)
    _record_check("drain_after_close")

    # 2) 回收 Python 侧引用；PySide6 在最后一个 Python 包装对象消失时销毁 C++ 对象
    try:
        gc.collect()
    except Exception as exc:
        _record_cleanup_error("gc_after_close", exc)
    _record_check("gc_after_close")
    try:
        _drain_qt_events(app)
    except Exception as exc:
        _record_cleanup_error("drain_after_gc", exc)
    _record_check("drain_after_gc")

    # 3) 退出 QApplication：此后不再有新的 Qt 事件投递
    try:
        app.quit()
    except Exception as exc:
        _record_cleanup_error("app_quit", exc)
    _record_check("app_quit")
    try:
        app.processEvents()
    except Exception as exc:
        _record_cleanup_error("app_process_events", exc)
    _record_check("app_process_events")
    try:
        gc.collect()
    except Exception as exc:
        _record_cleanup_error("gc_before_delete", exc)
    _record_check("gc_before_delete")
    # 显式把 QApplication 的 C++ 对象交给 DeferredDelete，避免解释器
    # finalization 阶段再销毁单例（实测 app.shutdown() 会直接 fastfail）。
    try:
        app.deleteLater()
    except Exception as exc:
        _record_cleanup_error("app_delete_later", exc)
    _record_check("app_delete_later")
    try:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    except Exception as exc:
        _record_cleanup_error("dispatch_deferred_delete", exc)
    _record_check("dispatch_deferred_delete")
    try:
        gc.collect()
    except Exception as exc:
        _record_cleanup_error("gc_after_delete", exc)
    _record_check("gc_after_delete")
    _record_check("qt_teardown_complete")


def pytest_runtest_teardown(item: Any, nextitem: Any) -> None:  # noqa: ARG001
    """在每个用例边界回收循环垃圾，避免 Qt 包装对象在无关控件构造中途被回收。

    第 38 轮 Clean 复现：0xC0000409 曾在 pyqtgraph ``AxisItem`` 构造过程中被
    Python GC 触发（Python 栈显示 Garbage-collecting），也在 finalization 出现。
    在测试边界显式收集，把循环垃圾回收从「任意 Qt 构造点」移到「用例结束点」；
    这是测试隔离措施，不宣称修复 PySide6/pyqtgraph 生命周期根因。
    """
    gc.collect()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:  # noqa: ARG001
    """所有测试与会话级夹具收尾之后，再做 Qt 有序销毁。

    不在此处强退：pytest 的其余收尾钩子和 Python finalization 必须真实执行。
    退出模式与 clean_exit_verified 由 scripts/pytest_runner.py 与
    scripts/run_with_timeout.py 外层监督器记录。
    """
    teardown_qt_application()
    _write_cleanup_report(exitstatus)
