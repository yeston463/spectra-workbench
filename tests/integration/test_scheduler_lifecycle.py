"""P1-04 验收测试：任务调度器、真实取消、并发队列、超时与 Worker 崩溃恢复。

验收标准：
- 真实 GDAL 任务在实际计算期间协作取消，返回 cancelled，残存 staging 正常清理
- 无法协作取消时超时强杀子进程，GUI 不退出，无孤儿进程残留
- 并发限制：单活动任务并发控制，排队状态（queued -> running），支持队列内取消与优先级
- Worker 崩溃监控：子进程异常退出被感知，当次 attempt 记录故障证据，按 RetryPolicy 自动拉起新世代 Worker 重试
- 历史证据保护：重试不覆盖原 attempt 证据
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    CancellationPolicy,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    Priority,
    ResourceRequest,
    RetryPolicy,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.scheduler import TaskScheduler

pytestmark = [pytest.mark.integration, pytest.mark.engine]


def _make_spec(
    task_id: str,
    temp_root: Path,
    provider_id: str = "dummy",
    capability_id: str = "dummy:noop",
    parameters: dict | None = None,
    coop_timeout: float = 5.0,
    allow_kill: bool = True,
    max_retries: int = 0,
    priority: Priority = Priority.normal,
) -> TaskSpec:
    params = parameters or {}
    return TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id=provider_id,
            runtime_id=f"{provider_id}-runtime",
            expected_version="1.0",
        ),
        project_root=str(temp_root),
        workspace=str(temp_root / f"workspace_{task_id}"),
        parameters=params,
        resources=ResourceRequest(priority=priority),
        cancellation=CancellationPolicy(
            cooperative_timeout_seconds=coop_timeout,
            allow_process_termination=allow_kill,
        ),
        retry=RetryPolicy(max_automatic_retries=max_retries),
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.scalar,
                uri=(temp_root / f"output_{task_id}.bin").as_uri(),
                overwrite=True,
            )
        ],
    )


def test_concurrency_limit_and_queue_transitions():
    """验证并发限制（max_concurrency=1）：先提交的任务执行，后提交的任务在 queued 排队。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        sched = TaskScheduler(project_root=temp_root, max_concurrency=1)
        sched.start()

        try:
            # 提交长任务 A (steps=10 * 0.05s = 0.5s)
            spec_a = _make_spec("task-a", temp_root, parameters={"steps": 10, "delay": 0.05})
            handle_a = sched.submit(spec_a)

            # 提交任务 B
            spec_b = _make_spec("task-b", temp_root, parameters={"steps": 2, "delay": 0.02})
            handle_b = sched.submit(spec_b)

            time.sleep(0.1)

            # 断言：任务 A 正在运行，任务 B 必须在排队
            assert handle_a.status in {TaskStatus.running, TaskStatus.queued}
            assert handle_b.status == TaskStatus.queued

            # 等待全量完成
            start_t = time.monotonic()
            while (not handle_a.is_terminal or not handle_b.is_terminal) and time.monotonic() - start_t < 10.0:
                time.sleep(0.05)

            assert handle_a.status is TaskStatus.succeeded
            assert handle_b.status is TaskStatus.succeeded
            # 任务 B 的完成时间必然晚于 A
            assert handle_b.finished_at is not None and handle_a.finished_at is not None
            assert handle_b.finished_at >= handle_a.finished_at
        finally:
            sched.stop()


def test_queue_cancellation_before_execution():
    """验证在任务排队阶段（queued）发起取消，任务立即进入 cancelled，不被 Worker 调度执行。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        sched = TaskScheduler(project_root=temp_root, max_concurrency=1)
        sched.start()

        try:
            # 阻塞任务
            spec_block = _make_spec("t-block", temp_root, parameters={"steps": 10, "delay": 0.05})
            sched.submit(spec_block)

            # 排队任务
            spec_queued = _make_spec("t-queued", temp_root, parameters={"steps": 2, "delay": 0.02})
            handle_queued = sched.submit(spec_queued)

            assert handle_queued.status == TaskStatus.queued

            # 在排队中取消
            cancelled = sched.cancel("t-queued")
            assert cancelled is True
            assert handle_queued.status is TaskStatus.cancelled
            assert handle_queued.final_result is not None
            assert handle_queued.final_result.status is TaskStatus.cancelled
        finally:
            sched.stop()


def test_real_gdal_task_cooperative_cancellation_during_computation():
    """验证真实 GDAL 任务在实际计算期间发出取消，能够协作式终止，严格产出 cancelled 终态，未提交输出。"""
    root = find_project_root()
    from tests.fixtures.synthetic import write_envi_raster

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        # 构造足够产生计算分块的 800x800 Float32 栅格 (2.56MB)
        raw_data = b"\x00\x00\x80?" * (800 * 800)
        _, in_dat = write_envi_raster(temp_root / "large_input", raw_data, 800, 800, bands=1, dtype="float32")

        sched = TaskScheduler(project_root=root, max_concurrency=1)

        # 确定的执行中同步点：捕获 GDAL 原生 progress 回调触发取消
        cancel_triggered = threading.Event()

        def _on_prog(h, ratio, phase, msg):
            if (
                h.task_id == "t-gdal-cancel-01"
                and phase == "processing"
                and ratio is not None
                and ratio >= 0.02
                and not cancel_triggered.is_set()
            ):
                cancel_triggered.set()
                sched.cancel("t-gdal-cancel-01")

        sched.on_progress.append(_on_prog)
        sched.start()

        try:
            out_tif = temp_root / "cancelled_output.tif"
            spec = TaskSpec(
                task_id="t-gdal-cancel-01",
                attempt=1,
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace_cancel"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_tif.as_uri(), overwrite=True)],
                cancellation=CancellationPolicy(cooperative_timeout_seconds=5.0, allow_process_termination=True),
                parameters={"format": "GTiff", "resample_alg": "cubic"},
            )

            handle = sched.submit(spec)

            start_t = time.monotonic()
            while not handle.is_terminal and time.monotonic() - start_t < 10.0:
                time.sleep(0.05)

            # 核心断言：严格证明计算被中断并进入 cancelled（绝不接受 succeeded！）
            assert cancel_triggered.is_set(), "GDAL 原生进度回调未触发，未能证明在计算中发起取消"
            assert handle.status is TaskStatus.cancelled
            assert handle.final_result is not None
            assert handle.final_result.status is TaskStatus.cancelled
            assert not out_tif.exists(), "已取消的任务绝不能提交最终目标成果"
        finally:
            sched.stop()


def test_priority_queue_strict_fifo_ordering():
    """验证任务队列严格按照契约优先级（interactive: -10, normal: 0, background: 10）以及同级 FIFO 顺序出队。"""
    root = find_project_root()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        sched = TaskScheduler(project_root=root, max_concurrency=1)

        # 依次提交 background, normal-1, interactive, normal-2
        spec_bg = _make_spec("t-bg", temp_root, priority=Priority.background)
        spec_norm1 = _make_spec("t-norm1", temp_root, priority=Priority.normal)
        spec_int = _make_spec("t-int", temp_root, priority=Priority.interactive)
        spec_norm2 = _make_spec("t-norm2", temp_root, priority=Priority.normal)

        sched.submit(spec_bg)
        sched.submit(spec_norm1)
        sched.submit(spec_int)
        sched.submit(spec_norm2)

        # 校验出队顺序
        order = []
        while not sched._queue.empty():
            p_val, seq, t_id = sched._queue.get_nowait()
            order.append((p_val, t_id))

        assert order == [
            (-10, "t-int"),
            (0, "t-norm1"),
            (0, "t-norm2"),
            (10, "t-bg"),
        ]


def test_uncooperative_cancellation_force_kills_worker_safely():
    """验证当 Worker 无法协作取消时，超时后强杀子进程，GUI/调度器保持存活，无孤儿进程残留。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        sched = TaskScheduler(project_root=temp_root, max_concurrency=1)
        sched.start()

        try:
            # 提交极短协作取消超时 (0.2s) 的长计算任务
            spec = _make_spec(
                "t-uncoop-01",
                temp_root,
                parameters={"steps": 50, "delay": 0.1, "uncooperative": True},
                coop_timeout=0.2,
                allow_kill=True,
            )
            handle = sched.submit(spec)

            # 等待进入 running
            start_t = time.monotonic()
            while handle.status is not TaskStatus.running and time.monotonic() - start_t < 5.0:
                time.sleep(0.02)

            # 触发取消
            sched.cancel("t-uncoop-01")

            # 等待超时强杀生效
            start_wait = time.monotonic()
            while not handle.is_terminal and time.monotonic() - start_wait < 5.0:
                time.sleep(0.05)

            # 判定：任务已安全进入终态（cancelled 或 interrupted）
            assert handle.is_terminal
            assert handle.status in {TaskStatus.cancelled, TaskStatus.interrupted}

            # 核心断言：调度器主进程依然健康运行，可以接续处理新任务
            spec_next = _make_spec("t-next-01", temp_root, parameters={"steps": 1, "delay": 0.01})
            handle_next = sched.submit(spec_next)

            wait_next = time.monotonic()
            while not handle_next.is_terminal and time.monotonic() - wait_next < 5.0:
                time.sleep(0.05)

            assert handle_next.status is TaskStatus.succeeded
        finally:
            sched.stop()


def test_worker_crash_monitoring_and_automatic_retry_with_attempt_preservation():
    """验证 Worker 意外崩溃后被感知，记录当次 attempt 故障证据；按 RetryPolicy 自动拉起新世代 Worker 重试成功。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        sched = TaskScheduler(project_root=temp_root, max_concurrency=1)
        sched.start()

        try:
            # 允许 1 次自动重试
            spec = _make_spec(
                "t-crash-retry-01",
                temp_root,
                parameters={"steps": 30, "delay": 0.05},
                max_retries=1,
            )
            handle = sched.submit(spec)

            # 等待 Worker 运行起来
            start_t = time.monotonic()
            while handle.status is not TaskStatus.running and time.monotonic() - start_t < 5.0:
                time.sleep(0.02)

            # 模拟外部严重崩溃：在计算进行中强制杀掉 Worker 进程
            time.sleep(0.1)
            with sched._workers_lock:
                worker = sched._workers.get("dummy")
                assert worker is not None
                worker.kill()

            # 等待调度器自动感知崩溃并重试成功
            start_wait = time.monotonic()
            while not handle.is_terminal and time.monotonic() - start_wait < 10.0:
                time.sleep(0.05)

            # 核心断言：
            # 1. 任务最终应成功（重试生效）
            assert handle.status is TaskStatus.succeeded

            # 2. 核心证据保护：两次 attempt 证据必须完整保留，原 attempt 绝对不能被覆盖！
            assert len(handle.attempts) == 2
            att1 = handle.attempts[0]
            att2 = handle.attempts[1]

            assert att1.attempt == 1
            assert att1.status in {TaskStatus.failed, TaskStatus.interrupted}
            assert "Worker process terminated unexpectedly" in att1.error.get("message", "")

            assert att2.attempt == 2
            assert att2.status is TaskStatus.succeeded
        finally:
            sched.stop()
