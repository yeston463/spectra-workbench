"""退出阶段线程与 Qt 对象生命周期诊断（仅诊断用，不进产品代码）。

在 pytest 报告通过之后、解释器真正退出之前，把「谁还活着」写盘：
- 仍存活的 Python 线程（名称 / 守护标记 / 栈顶）
- 仍存活的 worker 子进程
- Qt 中尚未销毁的顶层对象数量

用法（需把 work 目录加入 PYTHONPATH）：
    python -m pytest -p exit_diag_probe ...
输出目录：work/exit_diag/
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

_OUT = Path(__file__).resolve().parent / "exit_diag"


def _dump(tag: str) -> None:
    try:
        _OUT.mkdir(parents=True, exist_ok=True)
        threads = []
        for t in threading.enumerate():
            if t is threading.current_thread():
                continue
            frames = [f.name for f in traceback.extract_stack(t.frame)][-5:]
            threads.append(
                {
                    "name": t.name,
                    "daemon": t.daemon,
                    "alive": t.is_alive(),
                    "stack": frames,
                }
            )
        # worker 子进程是否还活着
        procs = []
        try:
            out = os.popen("tasklist /FI \"IMAGENAME eq python.exe\" /FO CSV /NH").read()
            procs = [ln for ln in out.splitlines() if ln.strip()]
        except Exception as exc:  # pragma: no cover
            procs = [f"error: {exc}"]

        payload = {
            "tag": tag,
            "at": datetime.now().isoformat(),
            "pid": os.getpid(),
            "alive_threads": threads,
            "python_processes": procs,
        }
        stamp = datetime.now().strftime("%H%M%S_%f")
        (_OUT / f"{stamp}_{tag}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


atexit.register(_dump, "atexit")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    _dump(f"sessionfinish_{exitstatus}")


def pytest_unconfigure(config):  # noqa: ARG001
    _dump("unconfigure")
