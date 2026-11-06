"""任务调度器与生命周期管理器（P1-04）。

职责：
1. 任务队列与并发限制：按优先级管理任务排队（queued -> validating -> running -> terminal）。
2. 真实长作业协作取消：发送 Kind.cancel 并由算法在计算期间响应终止。
3. 无法协作取消与强杀保护：超时后强制结束专用作业子进程，GUI 保持稳定不退出，无孤儿进程残留。
4. 超时监控：捕获超期运行任务并安全熔断。
5. Worker 崩溃监控与自动重试：
   - 实时感知子进程崩溃或意外退出；
   - 记录当次 attempt 故障证据（绝不覆盖历史 attempt）；
   - 根据 RetryPolicy 触发下一次 attempt；
   - 增量递增 generation 并重新拉起健康 Worker 进程。
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.protocol.messages import Envelope
from spectra_workbench.workers.process import WorkerProcess

logger = logging.getLogger(__name__)


@dataclass
class TaskHandle:
    """任务调度句柄，跟踪多 attempt 历史与当前状态。"""

    task_id: str
    spec: TaskSpec
    status: TaskStatus = TaskStatus.queued
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    progress_ratio: float | None = None
    progress_phase: str = ""
    progress_message: str = ""
    logs: list[tuple[str, str, str]] = field(default_factory=list)  # (level, message, stream)
    attempts: list[TaskResult] = field(default_factory=list)
    final_result: TaskResult | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancel_requested_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.terminal


class TaskScheduler:
    """引擎任务调度器。"""

    def __init__(
        self,
        project_root: Path | str | None = None,
        max_concurrency: int = 1,
    ):
        self.project_root = find_project_root(project_root)
        self.max_concurrency = max_concurrency

        # 任务注册表与排队队列
        self._tasks: dict[str, TaskHandle] = {}
        self._tasks_lock = threading.Lock()
        self._queue: queue.PriorityQueue[tuple[int, int, str]] = queue.PriorityQueue()
        self._seq_counter = itertools.count()

        # Worker 管理池 (provider_id -> WorkerProcess)
        self._workers: dict[str, WorkerProcess] = {}
        self._worker_generations: dict[str, int] = {}
        self._workers_lock = threading.Lock()

        # 事件监听回调
        self.on_status_change: list[Callable[[TaskHandle, TaskStatus, TaskStatus], None]] = []
        self.on_progress: list[Callable[[TaskHandle, float | None, str, str], None]] = []
        self.on_log: list[Callable[[TaskHandle, str, str, str], None]] = []
        self.on_completed: list[Callable[[TaskHandle, TaskResult], None]] = []

        self._stop_event = threading.Event()
        self._dispatch_threads: list[threading.Thread] = []

    def start(self) -> None:
        """启动调度线程。"""
        self._stop_event.clear()
        for i in range(self.max_concurrency):
            t = threading.Thread(target=self._worker_loop, name=f"scheduler-worker-{i}", daemon=True)
            t.start()
            self._dispatch_threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止调度器并释放全部 Worker 进程。"""
        self._stop_event.set()
        # 清空排队任务
        while not self._queue.empty():
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()

        for t in self._dispatch_threads:
            t.join(timeout=1.0)
        self._dispatch_threads.clear()

        # 终止所有 Worker
        with self._workers_lock:
            for w in self._workers.values():
                w.stop(timeout=timeout)
                w.kill()
            self._workers.clear()

    def get_task(self, task_id: str) -> TaskHandle | None:
        with self._tasks_lock:
            return self._tasks.get(task_id)

    def submit(self, spec: TaskSpec) -> TaskHandle:
        """向调度器提交任务并入队。"""
        with self._tasks_lock:
            if spec.task_id in self._tasks:
                raise ValueError(f"Task ID {spec.task_id} already exists")
            handle = TaskHandle(task_id=spec.task_id, spec=spec, status=TaskStatus.queued)
            self._tasks[spec.task_id] = handle

        # 优先级数值转换（数字越小越优先）：
        # 严格对齐 contracts/task.schema.json 枚举：interactive: -10, normal: 0, background: 10
        raw_p = getattr(spec.resources, "priority", "normal")
        p_str = raw_p.value if hasattr(raw_p, "value") else str(raw_p)
        priority_map = {
            "interactive": -10,
            "high": -10,  # 容错别名
            "normal": 0,
            "background": 10,
            "low": 10,  # 容错别名
        }
        p_val = priority_map.get(p_str.lower(), 0)
        seq = next(self._seq_counter)

        self._queue.put((p_val, seq, spec.task_id))
        self._fire_status_change(handle, TaskStatus.queued, TaskStatus.queued)
        return handle

    def cancel(self, task_id: str) -> bool:
        """取消指定任务。

        - 若任务仍在队列中，立即标记为 cancelled，不参与执行；
        - 若任务正在运行，置位取消信号并向 Worker 发起协作取消；超时后强杀。
        """
        handle = self.get_task(task_id)
        if handle is None or handle.is_terminal:
            return False

        handle.cancel_requested.set()
        if handle.cancel_requested_at is None:
            handle.cancel_requested_at = time.monotonic()

        if handle.status is TaskStatus.queued:
            self._set_task_status(handle, TaskStatus.cancelled)
            handle.final_result = TaskResult(
                task_id=handle.task_id,
                attempt=handle.spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "cancelled_in_queue", "message": "Task cancelled before starting"},
            )
            handle.attempts.append(handle.final_result)
            self._fire_completed(handle, handle.final_result)
            return True

        if handle.status is TaskStatus.running:
            self._set_task_status(handle, TaskStatus.cancelling)
            provider_id = handle.spec.engine.provider_id
            with self._workers_lock:
                worker = self._workers.get(provider_id)
                if worker and worker.is_alive:
                    worker.cancel_task(task_id)
            return True

        return True

    def _get_or_create_worker(self, provider_id: str) -> WorkerProcess:
        """获取可用 Worker 进程，若不存在或已死则拉起新 Worker。"""
        with self._workers_lock:
            worker = self._workers.get(provider_id)
            if worker is None or not worker.is_alive:
                gen = self._worker_generations.get(provider_id, 0)
                if worker is not None:
                    gen += 1
                self._worker_generations[provider_id] = gen

                worker = WorkerProcess(
                    provider_id=provider_id,
                    worker_id=f"sched-{provider_id}-w{gen}",
                    generation=gen,
                    project_root=self.project_root,
                )
                worker.start(timeout=15.0)
                self._workers[provider_id] = worker
            return worker

    def _worker_loop(self) -> None:
        """单个并发槽的调度主循环。"""
        while not self._stop_event.is_set():
            try:
                _, _, task_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            handle = self.get_task(task_id)
            if handle is None:
                continue

            # 若排队期间已被取消
            if handle.cancel_requested.is_set() or handle.is_terminal:
                continue

            self._execute_handle(handle)

    def _execute_handle(self, handle: TaskHandle) -> None:
        """执行单个任务句柄（支持多 attempt 自动重试）。"""
        spec = handle.spec
        provider_id = spec.engine.provider_id

        while not self._stop_event.is_set():
            if handle.cancel_requested.is_set():
                self._set_task_status(handle, TaskStatus.cancelled)
                handle.final_result = TaskResult(
                    task_id=handle.task_id,
                    attempt=handle.spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "cancelled_before_execution", "message": "Task cancelled before execution started"},
                )
                handle.attempts.append(handle.final_result)
                self._fire_completed(handle, handle.final_result)
                return

            worker = self._get_or_create_worker(provider_id)
            attempt_spec = handle.spec

            if handle.cancel_requested.is_set():
                self._set_task_status(handle, TaskStatus.cancelled)
                handle.final_result = TaskResult(
                    task_id=handle.task_id,
                    attempt=handle.spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "cancelled_before_execution", "message": "Task cancelled before execution started"},
                )
                handle.attempts.append(handle.final_result)
                self._fire_completed(handle, handle.final_result)
                return

            # 1. 状态流转 -> running (此时 Worker 进程已就绪)
            handle.started_at = time.monotonic()
            self._set_task_status(handle, TaskStatus.running)

            # 2. 构造回调
            def _on_prog(env: Envelope) -> None:
                ratio = env.payload.get("ratio")
                phase = env.payload.get("phase", "")
                msg = env.payload.get("message", "")
                handle.progress_ratio = ratio
                handle.progress_phase = phase
                handle.progress_message = msg
                for cb in self.on_progress:
                    with contextlib.suppress(Exception):
                        cb(handle, ratio, phase, msg)

            def _on_log(env: Envelope) -> None:
                lvl = env.payload.get("level", "INFO")
                msg = env.payload.get("message", "")
                strm = env.payload.get("stream", "engine")
                handle.logs.append((lvl, msg, strm))
                for cb in self.on_log:
                    with contextlib.suppress(Exception):
                        cb(handle, lvl, msg, strm)

            # 3. 异步监视与执行
            timeout_limit = 60.0  # 基础超时保护
            coop_timeout = handle.spec.cancellation.cooperative_timeout_seconds

            exec_result: TaskResult | None = None
            crash_detected = False

            try:
                # 启动一个监控取消的辅助线程
                def _cancel_monitor(target_worker: WorkerProcess = worker, coop_limit: float = coop_timeout) -> None:
                    cancelled_sent = False
                    while handle.status in {TaskStatus.running, TaskStatus.cancelling} and not self._stop_event.is_set():
                        if handle.cancel_requested.is_set():
                            if handle.cancel_requested_at is None:
                                handle.cancel_requested_at = time.monotonic()

                            if handle.status is not TaskStatus.cancelling:
                                self._set_task_status(handle, TaskStatus.cancelling)

                            if not cancelled_sent:
                                target_worker.cancel_task(handle.task_id)
                                cancelled_sent = True

                            elapsed = time.monotonic() - handle.cancel_requested_at
                            if elapsed >= coop_limit and handle.spec.cancellation.allow_process_termination:
                                logger.warning(
                                    f"Task {handle.task_id} cancellation timed out ({elapsed:.2f}s >= {coop_limit}s); killing worker."
                                )
                                target_worker.kill()
                                break
                        time.sleep(0.02)

                canceller_thread = threading.Thread(target=_cancel_monitor, daemon=True)
                # 纳入统一跟踪：此前该线程没有 join/stop，stop() 后可能残留守护
                # 线程到解释器退出（第 37 轮线程生命周期收口）。
                self._dispatch_threads.append(canceller_thread)
                canceller_thread.start()

                # 执行任务
                exec_result = worker.execute_task(
                    attempt_spec,
                    timeout=timeout_limit,
                    on_progress=_on_prog,
                    on_log=_on_log,
                )
            except Exception as exc:
                # 检查是否为 Worker 崩溃或被强杀
                if not worker.is_alive:
                    crash_detected = True
                    stderr_msg = "".join(worker.stderr_lines[-20:])
                    exec_result = TaskResult(
                        task_id=attempt_spec.task_id,
                        attempt=attempt_spec.attempt,
                        status=TaskStatus.interrupted if handle.cancel_requested.is_set() else TaskStatus.failed,
                        error={
                            "code": "worker_crashed_or_terminated",
                            "message": f"Worker process terminated unexpectedly: {exc}\n{stderr_msg}",
                        },
                    )
                else:
                    exec_result = TaskResult(
                        task_id=attempt_spec.task_id,
                        attempt=attempt_spec.attempt,
                        status=TaskStatus.failed,
                        error={"code": "execution_failed", "message": str(exc)},
                    )

            # 记录本轮 attempt 证据
            assert exec_result is not None
            handle.attempts.append(exec_result)

            # Worker 若已死，从池中移除并触发下一次按需重启
            if crash_detected or not worker.is_alive:
                with self._workers_lock:
                    self._workers.pop(provider_id, None)

            # 4. 判定是否满足重试条件
            max_retries = spec.retry.max_automatic_retries
            can_retry = (
                exec_result.status is TaskStatus.failed
                and not handle.cancel_requested.is_set()
                and not spec.retry.external_side_effects
                and len(handle.attempts) <= max_retries
            )

            if can_retry:
                # 保留旧 attempt 证据，新建下一次 attempt
                logger.info(f"Task {handle.task_id} failed on attempt {attempt_spec.attempt}. Auto-retrying...")
                handle.spec = handle.spec.next_attempt()
                continue  # 进入下一轮 attempt
            else:
                # 终态收敛
                handle.finished_at = time.monotonic()
                handle.final_result = exec_result
                terminal_status = exec_result.status
                self._set_task_status(handle, terminal_status)
                self._fire_completed(handle, exec_result)
                break

    def _set_task_status(self, handle: TaskHandle, new_status: TaskStatus) -> None:
        old_status = handle.status
        handle.status = new_status
        self._fire_status_change(handle, old_status, new_status)

    def _fire_status_change(self, handle: TaskHandle, old_s: TaskStatus, new_s: TaskStatus) -> None:
        for cb in self.on_status_change:
            with contextlib.suppress(Exception):
                cb(handle, old_s, new_s)

    def _fire_completed(self, handle: TaskHandle, result: TaskResult) -> None:
        for cb in self.on_completed:
            with contextlib.suppress(Exception):
                cb(handle, result)
