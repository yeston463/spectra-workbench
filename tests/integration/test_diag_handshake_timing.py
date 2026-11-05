"""在 pytest 内量化 worker 握手耗时是否随次数累积。

现象：同一组 32 个测试，单独跑 24s，组合跑 1102s，且失败集中在
`WorkerTimeoutError: Worker 启动握手超时 (20.0s)`（连续 18 次成功后开始超时）。
受控的独立进程脚本（无 Qt）测 12 次启动耗时恒定 0.5s，无累积。

本测试在 **pytest 进程内**（有 QApplication、事件循环、输出捕获）连续启动 worker，
逐次记录握手耗时与句柄/线程数，用于定位累积因素。

用 pytest 自身的退出码作为观测：pytest 进程退出码即是被测行为的一部分。
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.scheduler.client import QtWorkerClient

pytestmark = [pytest.mark.integration, pytest.mark.engine]


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_worker_handshake_timing_in_pytest(qapp: QApplication) -> None:
    """连续启动 N 次 worker，记录每次握手耗时。

    断言仅用于**输出观测数据**（耗时分布打印到 stdout 便于归档），
    不因耗时波动而失败 —— 该现象本身是环境相关，不作为功能断言。
    """
    root = find_project_root()
    n = 24
    timings: list[float] = []
    errors: list[str] = []

    for i in range(n):
        client = QtWorkerClient(
            provider_id="gdal", worker_id=f"timing-{i:03d}", project_root=str(root)
        )
        t0 = time.monotonic()
        try:
            client.start(timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        timings.append(time.monotonic() - t0)
        try:
            client.stop(timeout=5.0)
        except Exception:
            with contextlib.suppress(Exception):
                client.kill()
        client.deleteLater()
        qapp.processEvents()

    alive = [t.name for t in threading.enumerate() if t.is_alive()]
    print(
        "\n[握手耗时观测] "
        f"n={n} 成功={len(timings) - len(errors)} 失败={len(errors)} "
        f"首={timings[0]:.3f}s 末={timings[-1]:.3f}s "
        f"max={max(timings):.3f}s 存活线程={len(alive)}"
    )
    if errors:
        print(f"[握手耗时观测] 首个错误：{errors[0]}")

    # 观测输出（不做硬断言，避免把环境波动变成功能失败）
    assert len(timings) == n
