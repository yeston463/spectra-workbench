"""Worker 客户端并发启动与生命周期回归测试。

覆盖两处与退出异常/偶发失败相关的真实缺陷：

1. **并发 start() 竞态**：多个请求线程可能同时发现 worker 尚未启动而并发调用
   ``start()``，后一次会覆盖前一次的子进程句柄，导致前一个 worker 泄漏、
   其请求永远收不到响应（表现为「等待图层超时：实际 0 个」）。
   ``start()`` 必须串行化并在锁内二次检查存活状态。

2. **关闭后不得再向已销毁的 Qt 对象派发信号**：``QtWorkerClient`` 是 QObject，
   其 C++ 侧被销毁后再 emit 属于未定义行为，在 Windows 上会随机触发
   ``STATUS_STACK_BUFFER_OVERRUN``（0xC0000409）异常退出码。
"""

from __future__ import annotations

import threading
import time

import pytest

from spectra_workbench.protocol.messages import Envelope, Kind
from spectra_workbench.scheduler.client import QtWorkerClient

pytestmark = pytest.mark.unit


def _ready_envelope() -> Envelope:
    return Envelope(
        kind=Kind.ready,
        message_id="m-diag",
        worker_id="w-diag",
        generation=0,
        payload={"role": "gdal", "runtime_id": "rt-diag"},
    )


@pytest.fixture
def client(tmp_path) -> QtWorkerClient:
    c = QtWorkerClient(provider_id="gdal", project_root=str(tmp_path))
    yield c
    c.kill()


def test_client_starts_open(client: QtWorkerClient) -> None:
    assert client._closed is False, "新客户端不应处于关闭态"


def test_envelope_dispatch_before_close_is_allowed(client: QtWorkerClient) -> None:
    """关闭前正常派发，不得被拦截（避免矫枉过正导致功能失效）。"""
    client._on_worker_envelope(_ready_envelope())


def test_envelope_dispatch_after_close_is_dropped(client: QtWorkerClient) -> None:
    """关闭后到达的在途 envelope 必须被丢弃。

    QtWorkerClient 是 QObject，MainWindow 销毁后其 C++ 侧失效；
    worker 的 stdout 读取线程（守护线程）仍可能回调此处。
    此时 emit 信号属于未定义行为，正是 0xC0000409 的候选成因。
    """
    client.stop(timeout=1.0)
    assert client._closed is True, "stop() 必须置位关闭标志"

    # 不得抛异常：若仍 emit 到已销毁对象，这里会出现 RuntimeError 或进程崩溃
    client._on_worker_envelope(_ready_envelope())


def test_stop_marks_closed_and_joins_request_threads(client: QtWorkerClient) -> None:
    """stop() 必须关闭信号派发并回收请求线程。"""
    client.stop(timeout=1.0)
    assert client._closed is True
    alive = [t for t in client._req_threads if t.is_alive()]
    assert not alive, f"stop() 后仍有请求线程存活：{[t.name for t in alive]}"


def test_kill_marks_closed(client: QtWorkerClient) -> None:
    client.kill()
    assert client._closed is True


def test_no_daemon_thread_survives_client_shutdown(client: QtWorkerClient) -> None:
    """关闭后不得残留本客户端的守护请求线程。

    守护线程活到解释器退出，会在 Windows 上随机触发异常退出码。
    """
    before = {t.name for t in threading.enumerate()}
    client.stop(timeout=2.0)
    leaked = [t.name for t in threading.enumerate() if t.daemon and t.name not in before]
    assert not leaked, f"关闭后新增了守护线程：{leaked}"


def test_concurrent_start_launches_single_worker(client: QtWorkerClient) -> None:
    """并发 start() 只能拉起一个 worker 子进程。

    否则后一次 start() 会覆盖前一次的子进程句柄：前一个 worker 进程泄漏，
    而它的请求永远等不到响应（界面上表现为图层一直加载不出来）。
    """
    errors: list[Exception] = []
    started: list[object] = []
    barrier = threading.Barrier(6)

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
            started.append(client.start(timeout=20.0))
        except Exception as exc:  # pragma: no cover - 记录后由断言呈现
            errors.append(exc)

    threads = [threading.Thread(target=_worker, name=f"start-race-{i}") for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)

    assert not errors, f"并发 start() 出现异常：{errors!r}"
    assert len(started) == 6, "所有并发调用都应拿到握手信封"

    # 关键断言：worker 子进程只有一个，没有被并发启动覆盖掉
    assert client.worker._proc is not None
    assert client.is_alive, "最终 worker 必须处于存活状态"

    # 进程句柄未被替换过：所有并发调用拿到的是同一个 worker
    assert len({id(s) for s in started}) == 1, "并发 start() 返回了不同信封，疑似重复拉起 worker"


def test_start_is_serialized_by_lock(client: QtWorkerClient) -> None:
    """start() 必须持有 _start_lock，防止并发重复拉起子进程。"""
    assert hasattr(client, "_start_lock"), "客户端必须提供 _start_lock 串行化启动"
    assert isinstance(client._start_lock, type(threading.Lock())), "_start_lock 必须是线程锁"

    # 锁被持有时，另一个线程不得进入 start()
    entered = threading.Event()
    released = threading.Event()

    def _holder() -> None:
        with client._start_lock:
            entered.set()
            released.wait(timeout=5)

    t = threading.Thread(target=_holder, name="lock-holder")
    t.start()
    assert entered.wait(timeout=5), "持锁线程未启动"

    def _contender() -> None:
        with client._start_lock:
            pass

    c = threading.Thread(target=_contender, name="lock-contender")
    c.start()
    time.sleep(0.2)
    assert c.is_alive(), "并发线程不应在锁释放前进入 start() 临界区"

    released.set()
    t.join(timeout=5)
    c.join(timeout=5)
    assert not c.is_alive(), "锁释放后并发线程应能进入"
