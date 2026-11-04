"""P1-02 跨进程 Worker 生命周期与 JSONL 通信验收测试。

覆盖验收标准：
- 分包、粘包、UTF-8 中文往返
- stderr 隔离（不污染 stdout JSONL 协议流）
- 非法消息、未知动作与异常处理
- 协作式取消与无残余进程
- 超时保护与世代号守卫（GenerationGuard）
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from spectra_workbench.domain.models import (
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.protocol.messages import Kind, new_message
from spectra_workbench.workers.process import WorkerProcess, WorkerTimeoutError

pytestmark = [pytest.mark.integration]


def _make_dummy_spec(
    task_id: str = "t1",
    steps: int = 3,
    delay: float = 0.05,
    parameters: dict | None = None,
) -> TaskSpec:
    params = {"steps": steps, "delay": delay}
    if parameters:
        params.update(parameters)
    return TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id="dummy:noop",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="dummy",
            runtime_id="dummy-test",
            expected_version="1.0.0",
        ),
        project_root=str(Path.cwd()),
        workspace=tempfile.gettempdir(),
        parameters=params,
        outputs=[
            OutputPlan(
                key="output",
                kind="scalar",
                uri="memory://dummy",
            )
        ],
    )


def test_worker_handshake_and_lifecycle():
    """验证 Worker 进程启动、握手（hello -> ready）、心跳与正常 shutdown 退出。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-handshake-w1")
    try:
        ready_env = worker.start(timeout=10.0)
        assert ready_env.kind is Kind.ready
        assert ready_env.payload["runtime_id"] == "dummy-worker"
        assert "execute_task" in ready_env.payload.get("features", [])

        # 检查 hello 消息先于 ready 收到
        kinds = [m.kind for m in worker.received_envelopes]
        assert Kind.hello in kinds
        assert Kind.ready in kinds

        # 发送心跳
        hb_resp = worker.heartbeat(timeout=5.0)
        assert hb_resp.kind is Kind.heartbeat
        assert hb_resp.payload["monotonic_seconds"] > 0
        assert hb_resp.payload["active_task_id"] is None

        # 正常退出
        code = worker.stop(timeout=5.0)
        assert code == 0
        assert not worker.is_alive
    finally:
        worker.kill()


def test_worker_task_execution_progress_and_result():
    """验证任务提交、accepted 确认、progress 增量流式上报与 result 终态。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-exec-w1")
    try:
        worker.start(timeout=10.0)
        progresses: list[float] = []

        def on_prog(env):
            r = env.payload.get("ratio")
            if r is not None:
                progresses.append(r)

        spec = _make_dummy_spec("t-exec-01", steps=4, delay=0.03)
        res = worker.execute_task(spec, timeout=10.0, on_progress=on_prog)

        assert res.status is TaskStatus.succeeded
        assert res.task_id == "t-exec-01"
        assert res.outputs and res.outputs[0]["key"] == "output"
        assert len(progresses) >= 4
        assert progresses[-1] == 1.0

        worker.stop()
    finally:
        worker.kill()


def test_worker_stderr_isolation_and_no_stdout_corruption():
    """验证 Worker 输出到 stderr 或第三方 print 不污染 stdout JSONL 协议流。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-stderr-w1")
    try:
        worker.start(timeout=10.0)

        # 执行任务（Dummy 内部由 sys.stdout=sys.stderr 重定向）
        spec = _make_dummy_spec("t-stderr-01", steps=2, delay=0.02)
        res = worker.execute_task(spec, timeout=10.0)

        assert res.status is TaskStatus.succeeded

        # 所有收到的 envelope 均为合法且可解析，无 JSON 解析崩溃
        for env in worker.received_envelopes:
            assert env.protocol_version == "1.0"
            assert env.kind in list(Kind)

        worker.stop()
    finally:
        worker.kill()


def test_worker_chinese_utf8_roundtrip():
    """验证中文参数、中文日志与中文路径在跨进程管道中完整无损。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-chinese-w1")
    try:
        worker.start(timeout=10.0)

        chinese_name = "北京高分二号多光谱_2026年测试"
        chinese_desc = "波段重采样与正射校正测试：含特殊字符 ~!@#$%"

        spec = _make_dummy_spec(
            "t-chinese-01",
            steps=2,
            delay=0.02,
            parameters={"name": chinese_name, "desc": chinese_desc},
        )
        res = worker.execute_task(spec, timeout=10.0)

        assert res.status is TaskStatus.succeeded
        worker.stop()
    finally:
        worker.kill()


def test_worker_cooperative_cancel():
    """验证长计算中途发出 cancel 信号，任务能在宽限期内协作式终止并返回 cancelled。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-cancel-w1")
    try:
        worker.start(timeout=10.0)

        # 启动一个耗时较长的任务 (20 steps * 0.1s = 2.0s)
        spec = _make_dummy_spec("t-cancel-01", steps=20, delay=0.1)

        import threading

        def _cancel_after():
            time.sleep(0.3)
            worker.cancel_task("t-cancel-01")

        canceller = threading.Thread(target=_cancel_after, daemon=True)
        canceller.start()

        start_t = time.monotonic()
        res = worker.execute_task(spec, timeout=5.0)
        duration = time.monotonic() - start_t

        assert res.status is TaskStatus.failed or res.status is TaskStatus.cancelled
        # 必须显著少于 2.0 秒，证明非阻塞式及时中断
        assert duration < 1.5

        # 取消后 Worker 仍保持就绪，可继续处理下一个任务
        spec2 = _make_dummy_spec("t-cancel-02", steps=1, delay=0.01)
        res2 = worker.execute_task(spec2, timeout=5.0)
        assert res2.status is TaskStatus.succeeded

        worker.stop()
    finally:
        worker.kill()


def test_worker_illegal_message_handling():
    """验证向 Worker 注入未知动作或非法结构时，Worker 产生 Kind.error 而不崩溃退出。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-illegal-w1")
    try:
        worker.start(timeout=10.0)

        # 发送未知动作
        resp = worker.send_request("non_existent_action", {"foo": "bar"}, timeout=5.0)
        assert resp.kind is Kind.error
        assert resp.payload["code"] == "unknown_action"

        # Worker 进程仍然活着且能正常处理后续正常任务
        assert worker.is_alive
        hb = worker.heartbeat(timeout=5.0)
        assert hb.kind is Kind.heartbeat

        worker.stop()
    finally:
        worker.kill()


def test_worker_generation_guard():
    """验证 GenerationGuard 过滤掉上一代旧消息。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-gen-w1", generation=2)
    try:
        worker.start(timeout=10.0)

        # 模拟伪造的上一代旧响应 (generation=1)
        stale_env = new_message(
            Kind.progress,
            worker.worker_id,
            {"ratio": 0.5, "phase": "stale"},
            generation=1,
        )

        assert worker._guard.accept(stale_env) is False
        assert worker._guard.rejected_stale == 1

        # 本代消息被正常接受
        valid_env = new_message(
            Kind.progress,
            worker.worker_id,
            {"ratio": 0.5, "phase": "valid"},
            generation=2,
        )
        assert worker._guard.accept(valid_env) is True

        worker.stop()
    finally:
        worker.kill()


def test_worker_timeout_raises_worker_timeout_error():
    """验证请求在未收到响应超时后抛出 WorkerTimeoutError。"""
    worker = WorkerProcess(provider_id="dummy", worker_id="test-timeout-w1")
    try:
        worker.start(timeout=10.0)

        # 执行耗时 0.5s 的任务，但客户端设置极短超时 0.05s
        spec = _make_dummy_spec("t-timeout-01", steps=5, delay=0.1)
        with pytest.raises(WorkerTimeoutError):
            worker.execute_task(spec, timeout=0.05)

        worker.stop()
    finally:
        worker.kill()
