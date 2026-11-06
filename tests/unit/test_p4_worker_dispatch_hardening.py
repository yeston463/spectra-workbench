"""第 37 轮疑难项回归：跨线程信号收敛、查询错误隔离、握手状态与线程清理。

这些测试固定的是**机制**而非现象：即使某台机器上没有复现 0xC0000409，
也能证明「worker 读取线程不再触碰 Qt」「查询失败不再污染任务面板」
「过期握手不会被复用」「读者线程先 join 后关管道」。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from spectra_workbench.protocol.messages import Envelope, Kind
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.workers.process import WorkerProcess

pytestmark = pytest.mark.unit


def _envelope(kind: Kind, payload: dict[str, Any]) -> Envelope:
    return Envelope(
        kind=kind,
        message_id="m-hardening",
        worker_id="w-hardening",
        generation=0,
        payload=payload,
    )


@pytest.fixture
def client(tmp_path: Any) -> Any:
    c = QtWorkerClient(provider_id="dummy", project_root=tmp_path)
    yield c
    c.kill()


def test_reader_thread_envelopes_are_queued_not_emitted(client: QtWorkerClient) -> None:
    """读取线程只入队；Qt 信号必须由 GUI 线程的 drain 发出。"""
    seen: list[dict[str, Any]] = []
    client.sig_ready.connect(seen.append)

    ready = _envelope(Kind.ready, {"runtime_id": "dummy-worker"})
    reader = threading.Thread(
        target=client._on_worker_envelope, args=(ready,), name="fake-stdout-reader"
    )
    reader.start()
    reader.join(timeout=2.0)

    assert not reader.is_alive()
    assert seen == [], "读取线程不得直接 emit Qt 信号（会分配外部线程 TLS）"
    assert client._signal_queue.qsize() == 1

    client._drain_signal_queue()  # 模拟 GUI 线程的 QTimer 回调
    assert len(seen) == 1 and seen[0]["runtime_id"] == "dummy-worker"


def test_query_error_is_not_broadcast(client: QtWorkerClient) -> None:
    """只读查询失败走定向 on_error，不再全客户端广播污染任务面板。"""
    errors: list[tuple[str, str]] = []
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))

    client._on_worker_envelope(
        _envelope(
            Kind.error,
            {
                "code": "probe_vector_failed",
                "message": "无法打开矢量数据集",
                "retryable": False,
                "category": "query",
            },
        )
    )
    client._drain_signal_queue()
    assert errors == []
    assert client._signal_queue.empty()


def test_task_error_is_still_broadcast(client: QtWorkerClient) -> None:
    """任务级错误没有 category=query，必须照常广播（不得矫枉过正）。"""
    errors: list[tuple[str, str]] = []
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))

    client._on_worker_envelope(
        _envelope(
            Kind.error,
            {"code": "executor_internal_error", "message": "boom", "retryable": False},
        )
    )
    client._drain_signal_queue()
    assert errors == [("executor_internal_error", "boom")]


def test_closed_client_drops_late_envelopes(client: QtWorkerClient) -> None:
    """关闭后到达的在途消息必须丢弃，队列也不得堆积。"""
    client.stop(timeout=1.0)
    client._on_worker_envelope(_envelope(Kind.ready, {"runtime_id": "late"}))
    assert client._signal_queue.empty(), "关闭态不得再入队"


def test_restart_does_not_reuse_stale_handshake(tmp_path: Any) -> None:
    """超时/崩溃后复用同一 WorkerProcess，不得返回上一代的 ready。"""
    stale = _envelope(Kind.ready, {"runtime_id": "STALE-FROM-PREVIOUS-GENERATION"})
    worker = WorkerProcess(provider_id="dummy", worker_id="hardening-restart")
    worker._ready_envelope = stale
    worker.received_envelopes.append(stale)
    try:
        fresh = worker.start(timeout=10.0)
        assert fresh.kind is Kind.ready
        assert fresh.payload["runtime_id"] == "dummy-worker"
        assert fresh is not stale
        assert stale not in worker.received_envelopes
    finally:
        worker.stop(timeout=5.0)


def test_join_readers_joins_threads_before_closing_pipes() -> None:
    """Windows 上先关管道会让阻塞的 readline 线程行为不确定，必须先 join。"""
    events: list[str] = []

    class _Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            events.append(f"close:{self.name}")

    class _Thread:
        def __init__(self, name: str) -> None:
            self.name = name
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
            events.append(f"join:{self.name}")
            self._alive = False

    class _Proc:
        stdout = _Stream("stdout")
        stderr = _Stream("stderr")
        stdin = _Stream("stdin")

    worker = WorkerProcess(provider_id="dummy", worker_id="hardening-join")
    worker._proc = _Proc()  # type: ignore[assignment]
    worker._stdout_thread = _Thread("stdout")  # type: ignore[assignment]
    worker._stderr_thread = _Thread("stderr")  # type: ignore[assignment]

    worker._join_readers()

    assert events.index("join:stdout") < events.index("close:stdout")
    assert events.index("join:stderr") < events.index("close:stderr")
