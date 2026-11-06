"""S2: Worker lifecycle state machine regressions.

Covers concurrent start sharing, stop during start, handshake timeout followed
by restart, early exit, stale generation rejection and bounded start/stop
cycles with thread/handle convergence.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.protocol.messages import Envelope, Kind, new_message
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.workers.process import (
    WorkerProcess,
    WorkerProcessError,
    WorkerState,
    WorkerTimeoutError,
)

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


def _wait_for(predicate, timeout: float = 10.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{label}")


def _handle_count() -> int:
    count = ctypes.c_uint32(0)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return int(count.value)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _dummy_worker(worker_id: str) -> WorkerProcess:
    return WorkerProcess(provider_id="dummy", worker_id=worker_id, project_root=ROOT)


def test_concurrent_start_shares_one_handshake() -> None:
    worker = _dummy_worker("s2-concurrent")
    barrier = threading.Barrier(8)
    results: list[Envelope] = []
    errors: list[BaseException] = []

    def _run() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(worker.start(timeout=15.0))
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_run, name=f"s2-start-{i}") for i in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not errors, errors
        assert len(results) == 8
        assert len({id(env) for env in results}) == 1, "并发 start 必须共享同一个 ready 对象"
        assert worker.state is WorkerState.READY
        assert worker._proc is not None and worker._proc.poll() is None
        timings = worker.startup_timings
        assert timings["spawn_start"] <= timings["spawn_done"] <= timings["hello"] <= timings["ready"]
        assert timings["generation"] == worker.generation
    finally:
        worker.kill()


def test_stop_during_starting_kills_process_and_converges() -> None:
    worker = _dummy_worker("s2-stop-starting")
    gate = threading.Event()
    original_wait = worker._wait_ready
    errors: list[BaseException] = []

    def _blocked_wait(proc, generation, stop_event, deadline, timeout):  # noqa: ANN001
        gate.wait(timeout=10.0)
        return original_wait(proc, generation, stop_event, deadline, timeout)

    worker._wait_ready = _blocked_wait  # type: ignore[method-assign]

    def _run() -> None:
        try:
            worker.start(timeout=15.0)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    thread = threading.Thread(target=_run, name="s2-start-then-stop")
    try:
        thread.start()
        _wait_for(lambda: worker.state is WorkerState.STARTING, label="进入 STARTING")
        worker.stop(timeout=5.0)
        gate.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert errors, "启动中停止必须让 start 失败，而不是返回 ready"
        assert worker.state is WorkerState.STOPPED
        assert worker._proc is None or worker._proc.poll() is not None
        assert not any(t.name.startswith(f"worker-{worker.worker_id}-") for t in threading.enumerate())
    finally:
        gate.set()
        worker.kill()


def test_timeout_then_restart_uses_new_generation() -> None:
    worker = _dummy_worker("s2-timeout-restart")
    original_wait = worker._wait_ready
    calls = 0
    old_generation = None
    old_pid = None

    def _timeout_once(proc, generation, stop_event, deadline, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkerTimeoutError("injected handshake timeout")
        return original_wait(proc, generation, stop_event, deadline, timeout)

    worker._wait_ready = _timeout_once  # type: ignore[method-assign]
    try:
        with pytest.raises(WorkerTimeoutError, match="injected"):
            worker.start(timeout=15.0)
        old_generation = worker.generation
        old_pid = worker._proc.pid if worker._proc is not None else None
        assert worker.state is WorkerState.FAILED
        assert worker._proc is None or worker._proc.poll() is not None
        assert not any(
            t.name.startswith(f"worker-{worker.worker_id}-") for t in threading.enumerate()
        )

        worker._wait_ready = original_wait  # type: ignore[method-assign]
        ready = worker.start(timeout=15.0)
        assert ready.kind is Kind.ready
        assert worker.state is WorkerState.READY
        assert worker.generation > old_generation
        assert worker._proc is not None and worker._proc.pid != old_pid
    finally:
        worker.kill()


def test_early_exit_is_reported_and_cleaned(tmp_path: Path) -> None:
    package = tmp_path / "early" / "spectra_workbench" / "workers"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "entrypoint.py").write_text(
        "import sys\nsys.exit(7)\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path / "early") + os.pathsep + str(ROOT / "src")
    worker = WorkerProcess(
        provider_id="dummy",
        worker_id="s2-early-exit",
        project_root=ROOT,
        custom_python=sys.executable,
        custom_env=env,
    )
    try:
        with pytest.raises(WorkerProcessError, match="提前退出"):
            worker.start(timeout=10.0)
        assert worker.state is WorkerState.FAILED
        assert worker._proc is None or worker._proc.poll() is not None
        assert not any(
            t.name.startswith(f"worker-{worker.worker_id}-") for t in threading.enumerate()
        )
    finally:
        worker.kill()


def test_late_old_ready_is_rejected() -> None:
    worker = _dummy_worker("s2-late-ready")
    try:
        first = worker.start(timeout=15.0)
        first_generation = first.generation
        worker.stop(timeout=5.0)
        second = worker.start(timeout=15.0)
        assert second.generation != first_generation
        current = worker._ready_envelope
        stale = new_message(
            Kind.ready,
            worker.worker_id,
            {"runtime_id": "stale"},
            generation=first_generation,
        )
        worker._on_envelope_received(stale)
        assert worker._ready_envelope is current
        assert worker._guard.rejected_stale >= 1
        assert worker.state is WorkerState.READY
    finally:
        worker.kill()


def test_start_stop_cycles_converge_threads_and_handles() -> None:
    worker = _dummy_worker("s2-cycles")
    handles_before = _handle_count()
    pids: list[int] = []
    try:
        for _ in range(4):
            worker.start(timeout=15.0)
            assert worker.state is WorkerState.READY
            assert worker._proc is not None
            pids.append(worker._proc.pid)
            worker.stop(timeout=5.0)
            assert worker.state is WorkerState.STOPPED
            assert worker._proc is None or worker._proc.poll() is not None
            assert not any(
                t.name.startswith(f"worker-{worker.worker_id}-")
                for t in threading.enumerate()
            )
        assert len(set(pids)) == len(pids), "每次启动必须是独立进程"
        handles_after = _handle_count()
        assert handles_after - handles_before <= 8, (
            f"句柄增长异常：{handles_before} -> {handles_after}"
        )
    finally:
        worker.kill()


def test_concurrent_cycles_converge() -> None:
    """4 个 Worker 各 3 轮并发启动/停止：无异常、无残留线程/进程。"""
    workers = [_dummy_worker(f"s2-pressure-{i}") for i in range(4)]
    barrier = threading.Barrier(len(workers))
    errors: list[BaseException] = []
    pids: dict[int, list[int]] = {i: [] for i in range(len(workers))}
    handles_before = _handle_count()

    def _run(index: int) -> None:
        worker = workers[index]
        try:
            barrier.wait(timeout=5)
            for _ in range(3):
                worker.start(timeout=15.0)
                assert worker.state is WorkerState.READY
                assert worker._proc is not None
                pids[index].append(worker._proc.pid)
                worker.stop(timeout=5.0)
                assert worker.state is WorkerState.STOPPED
                assert worker._proc is None or worker._proc.poll() is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=(i,), name=f"s2-pressure-thread-{i}")
        for i in range(len(workers))
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors
        assert all(not t.is_alive() for t in threads)
        for index, worker in enumerate(workers):
            assert worker.state is WorkerState.STOPPED
            assert len(pids[index]) == 3
            assert len(set(pids[index])) == 3, "每轮启动必须是独立进程"
            assert not any(
                t.name.startswith(f"worker-{worker.worker_id}-")
                for t in threading.enumerate()
            )
        handles_after = _handle_count()
        assert handles_after - handles_before <= 24, (
            f"并发循环句柄增长异常：{handles_before} -> {handles_after}"
        )
    finally:
        for worker in workers:
            worker.kill()


def test_qt_client_concurrent_start_uses_one_worker(qapp: QApplication) -> None:
    client = QtWorkerClient(provider_id="dummy", project_root=ROOT)
    barrier = threading.Barrier(6)
    results: list[Envelope] = []
    errors: list[BaseException] = []

    def _run() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(client.start(timeout=15.0))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_run, name=f"s2-client-start-{i}") for i in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not errors, errors
        assert len(results) == 6
        assert client.worker._proc is not None
        assert client.worker.state is WorkerState.READY
        assert len({env.payload.get("runtime_id") for env in results}) == 1
    finally:
        client.stop(timeout=5.0)
