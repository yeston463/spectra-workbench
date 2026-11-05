"""worker 进程生命周期兜底的回归测试。

诊断背景（第 30 轮末尾的复现实验，结论含**否定性**部分）：
- 单个 GDAL worker 进程 RSS ≈ **66.4 MB**（加载 QGIS 的 osgeo）；
- 受控环境下连续启动 24 次 worker，握手耗时**恒定 0.56–0.63s**、零失败
  → worker 启动本身不慢、**无累积**；
- 并发跑另一个 worker 负载**未**触发握手超时；
- 残留 61 个读取守护线程**未**触发 0xC0000409；
- 未回收的 worker 子进程在基线下也**未能稳定复现**（worker 读到 stdin EOF 会自行退出）。

即：这些现象**高度依赖当时的系统状态**（该时段本机空闲内存仅剩 1.96 GB），
受控环境下无法复现，**根因未最终定位**。

但「异常路径下忘记回收 worker」始终是一个**可消除的失败模式**：
每个 worker 常驻约 66 MB，一旦累积，新 worker 的启动握手会因内存分配困难而超时
（`WorkerTimeoutError: Worker 启动握手超时`）。本轮为此加三层兜底并固化测试：

1. `WorkerProcess.__del__`：对象回收时兜底 kill 子进程；
2. `WorkerProcess.stop()`：进程未存活时也确保 kill；
3. `QtWorkerClient` 支持 `with` 语法，结构上保证清理。
"""

from __future__ import annotations

import gc
import os
import sys
import time
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.workers.process import WorkerProcess

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _alive_pids(pids: list[int]) -> int:
    """统计给定 pid 中**仍然存活**的数量（纯 Python，不依赖外部命令）。

    Windows 用 OpenProcess(SYNCHRONIZE) + WaitForSingleObject 判断；
    POSIX 用 os.kill(pid, 0)。
    """
    alive = 0
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        for pid in pids:
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                continue
            try:
                code = wt.DWORD()
                waited = kernel32.WaitForSingleObject(handle, 0) == 0
                if (
                    waited
                    and kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    and code.value == STILL_ACTIVE
                ):
                    alive += 1
            finally:
                kernel32.CloseHandle(handle)
        return alive
    import os as _os

    for pid in pids:
        try:
            _os.kill(pid, 0)
            alive += 1
        except (OSError, ProcessLookupError):
            pass
    return alive


def test_worker_process_del_kills_child(qapp: QApplication) -> None:
    """WorkerProcess 被回收时必须调用 kill 终止子进程（兜底防孤儿）。"""
    worker = WorkerProcess(
        provider_id="gdal", worker_id="del-guard-test", project_root=str(ROOT)
    )
    worker.start(timeout=30.0)
    assert worker._proc is not None
    assert worker.is_alive

    with patch.object(worker._proc, "kill", wraps=worker._proc.kill) as kill_spy:
        # 触发 __del__ 兜底
        worker.__del__()

    assert kill_spy.called, "WorkerProcess.__del__ 必须调用 kill 终止子进程"
    # 子进程确实已终止
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if worker._proc.poll() is not None:
            break
        time.sleep(0.05)
    assert worker._proc.poll() is not None, "子进程应已退出"


def test_worker_process_stop_kills_when_not_alive(qapp: QApplication) -> None:
    """stop() 在进程「未存活」分支也必须确保 kill，避免子进程残留。"""
    worker = WorkerProcess(
        provider_id="gdal", worker_id="stop-guard-test", project_root=str(ROOT)
    )
    worker.start(timeout=30.0)
    assert worker._proc is not None

    # 模拟「is_alive 判断与真实状态不一致」：直接把进程标记为已退出，
    # 但子进程仍活着，走 stop() 的未存活分支。
    with patch.object(WorkerProcess, "is_alive", property(lambda self: False)):
        worker.stop(timeout=1.0)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if worker._proc.poll() is not None:
            break
        time.sleep(0.05)
    assert worker._proc.poll() is not None, "stop() 的未存活分支也必须终止子进程"


def test_qt_worker_client_supports_context_manager(qapp: QApplication) -> None:
    """QtWorkerClient 支持 `with` 语法，退出上下文时自动清理。"""
    with QtWorkerClient(
        provider_id="gdal", worker_id="ctx-guard-test", project_root=str(ROOT)
    ) as client:
        client.start(timeout=30.0)
        assert client.is_alive
        worker_proc = client.worker._proc
        assert worker_proc is not None

    # 退出 with 后子进程应已终止
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if worker_proc.poll() is not None:
            break
        time.sleep(0.05)
    assert worker_proc.poll() is not None, "上下文管理器退出后子进程应已终止"


def test_no_orphan_worker_processes_after_context_managers(qapp: QApplication) -> None:
    """连续使用上下文管理器后，不应留下孤儿 worker 进程。

    只统计**本次测试启动过**的 pid（比全机扫描更精确）。
    """
    started_pids: list[int] = []
    for i in range(3):
        with QtWorkerClient(
            provider_id="gdal", worker_id=f"ctx-orphan-{i}", project_root=str(ROOT)
        ) as client:
            client.start(timeout=30.0)
            proc = client.worker._proc
            assert proc is not None
            started_pids.append(proc.pid)
    gc.collect()
    qapp.processEvents()
    time.sleep(1.0)

    alive = _alive_pids(started_pids)
    assert alive == 0, f"上下文管理器退出后仍有 {alive}/{len(started_pids)} 个 worker 存活（孤儿进程）"
