"""Qt 异步 Worker 客户端（P1-02 / P1-03）。

在 PySide6 UI 线程与后台 WorkerProcess 之间建立桥梁：
- 将跨进程 JSONL 事件（progress / log / result / error）转换为 Qt 信号，确保 UI 线程安全。
- 任务执行在后台工作线程提交，UI 事件循环保持响应（不卡顿）。
- 提供任务提交流程、进度条绑定与协作取消操作。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from spectra_workbench.domain.models import TaskResult, TaskSpec
from spectra_workbench.protocol.messages import Envelope, Kind
from spectra_workbench.workers.process import WorkerProcess


class QtWorkerClient(QObject):
    """基于 PySide6 QObject 信号槽机制的 Worker 客户端。"""

    # 声明 Qt 信号
    sig_hello = Signal(dict)
    sig_ready = Signal(dict)
    sig_progress = Signal(float, str, str)  # ratio, phase, message
    sig_log = Signal(str, str, str)  # level, message, stream
    sig_result = Signal(object)  # TaskResult
    sig_error = Signal(str, str)  # code, message
    sig_worker_stopped = Signal(int)  # exit code
    sig_dataset_probed = Signal(dict)
    sig_window_read = Signal(dict)

    # 内部排队信号：确保异步回调切回 Qt GUI 主线程执行 (req_id, payload, is_error)
    _sig_probe_dispatch = Signal(str, object, bool)
    _sig_read_dispatch = Signal(str, object, bool)
    _sig_call_dispatch = Signal(str, object, bool)

    def __init__(
        self,
        provider_id: str,
        worker_id: str | None = None,
        project_root: Path | str | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.provider_id = provider_id
        self._worker = WorkerProcess(
            provider_id=provider_id,
            worker_id=worker_id,
            project_root=project_root,
        )
        self._worker.add_listener(self._on_worker_envelope)
        self._active_task_id: str | None = None

        # 回调注册表与互斥锁（生命周期绑定单次请求）
        self._probe_callbacks: dict[str, tuple[Callable[[dict[str, Any]], None] | None, Callable[[str], None] | None]] = {}
        self._read_callbacks: dict[str, tuple[Callable[[dict[str, Any]], None] | None, Callable[[str], None] | None]] = {}
        self._call_callbacks: dict[str, tuple[Callable[[dict[str, Any]], None] | None, Callable[[str], None] | None]] = {}
        self._callbacks_lock = threading.Lock()
        self._req_threads: list[threading.Thread] = []

        # 连接内部排队信号，槽函数在 QObject 所属线程（主线程）执行
        self._sig_probe_dispatch.connect(self._on_probe_dispatched)
        self._sig_read_dispatch.connect(self._on_read_dispatched)
        self._sig_call_dispatch.connect(self._on_call_dispatched)

    @property
    def worker(self) -> WorkerProcess:
        return self._worker

    @property
    def is_alive(self) -> bool:
        return self._worker.is_alive

    def start(self, timeout: float = 15.0) -> Envelope:
        """同步启动 Worker 并建立握手（通常在后台初始化或快速启动）。"""
        return self._worker.start(timeout=timeout)

    def _spawn_thread(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._req_threads = [t for t in self._req_threads if t.is_alive()]
        self._req_threads.append(thread)
        thread.start()

    def _join_request_threads(self, timeout: float = 2.0) -> None:
        """关闭前等未完成的请求线程收尾。

        守护线程若活到解释器退出，Windows 上会随机触发 STATUS_STACK_BUFFER_OVERRUN，
        表现就是测试与关窗时的异常退出码。
        """
        deadline = time.monotonic() + timeout
        for thread in list(self._req_threads):
            remaining = max(0.0, deadline - time.monotonic())
            if thread.is_alive():
                thread.join(timeout=remaining)
        self._req_threads = [t for t in self._req_threads if t.is_alive()]

    def _on_worker_envelope(self, env: Envelope) -> None:
        """从 Worker 读取线程派发消息至 Qt 信号。"""
        if env.kind is Kind.hello:
            self.sig_hello.emit(env.payload)
        elif env.kind is Kind.ready:
            self.sig_ready.emit(env.payload)
        elif env.kind is Kind.progress:
            ratio = env.payload.get("ratio")
            r_val = float(ratio) if ratio is not None else -1.0
            phase = env.payload.get("phase", "")
            msg = env.payload.get("message", "")
            self.sig_progress.emit(r_val, phase, msg)
        elif env.kind is Kind.log:
            level = env.payload.get("level", "INFO")
            msg = env.payload.get("message", "")
            stream = env.payload.get("stream", "engine")
            self.sig_log.emit(level, msg, stream)
        elif env.kind is Kind.error:
            code = env.payload.get("code", "unknown_error")
            msg = env.payload.get("message", "")
            self.sig_error.emit(code, msg)

    def submit_task_async(self, spec: TaskSpec, timeout: float = 60.0) -> None:
        """异步在后台线程提交任务执行，结果通过 sig_result / sig_error 信号发射。"""
        self._active_task_id = spec.task_id

        def _worker_runner() -> None:
            try:
                if not self.is_alive:
                    self.start()
                result = self._worker.execute_task(spec, timeout=timeout)
                self.sig_result.emit(result)
            except Exception as exc:
                self.sig_error.emit("task_submission_error", str(exc))
            finally:
                self._active_task_id = None

        self._spawn_thread(_worker_runner, f"qt-submit-{spec.task_id}")

    def execute_task_sync(self, spec: TaskSpec, timeout: float = 60.0) -> TaskResult:
        """同步执行任务（供无事件循环或同步测试使用）。"""
        if not self.is_alive:
            self.start()
        self._active_task_id = spec.task_id
        try:
            return self._worker.execute_task(spec, timeout=timeout)
        finally:
            self._active_task_id = None

    def probe_dataset(self, path: Path | str, timeout: float = 10.0) -> dict[str, Any]:
        """同步向 Worker 发送探测请求并返回元数据字典。"""
        if not self.is_alive:
            self.start()
        return self._worker.probe_dataset(path, timeout=timeout)

    def _on_probe_dispatched(self, req_id: str, payload: Any, is_error: bool) -> None:
        """在 GUI 主线程安全分发 probe 回调。"""
        with self._callbacks_lock:
            cb = self._probe_callbacks.pop(req_id, None)
        if cb is None:
            return
        on_success, on_error = cb
        if is_error:
            if on_error:
                on_error(str(payload))
        elif on_success and isinstance(payload, dict):
            on_success(payload)

    def _on_read_dispatched(self, req_id: str, payload: Any, is_error: bool) -> None:
        """在 GUI 主线程安全分发 read_window 回调。"""
        with self._callbacks_lock:
            cb = self._read_callbacks.pop(req_id, None)
        if cb is None:
            return
        on_success, on_error = cb
        if is_error:
            if on_error:
                on_error(str(payload))
        elif on_success and isinstance(payload, dict):
            on_success(payload)

    def probe_dataset_async(
        self,
        path: Path | str,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """异步在后台线程向 Worker 探测数据集，回调在 GUI 主线程执行。"""
        req_id = uuid.uuid4().hex
        with self._callbacks_lock:
            self._probe_callbacks[req_id] = (on_success, on_error)

        def _runner() -> None:
            try:
                if not self.is_alive:
                    self.start()
                info = self._worker.probe_dataset(path, timeout=timeout)
                self.sig_dataset_probed.emit(info)
                self._sig_probe_dispatch.emit(req_id, info, False)
            except Exception as exc:
                self.sig_error.emit("probe_error", str(exc))
                self._sig_probe_dispatch.emit(req_id, str(exc), True)

        self._spawn_thread(_runner, f"qt-probe-{Path(path).stem}")

    def read_window(
        self,
        path: Path | str,
        x_off: int = 0,
        y_off: int = 0,
        win_w: int = 512,
        win_h: int = 512,
        out_w: int = 512,
        out_h: int = 512,
        bands: list[int] | None = None,
        timeout: float = 15.0,
        raw: bool = False,
        overview: str = "auto",
    ) -> dict[str, Any]:
        """同步向 Worker 发送分块读取请求。raw=True 返回原值与有效性位图。"""
        if not self.is_alive:
            self.start()
        return self._worker.read_window(
            path=path,
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            bands=bands,
            timeout=timeout,
            raw=raw,
            overview=overview,
        )

    def read_window_async(
        self,
        path: Path | str,
        x_off: int = 0,
        y_off: int = 0,
        win_w: int = 512,
        win_h: int = 512,
        out_w: int = 512,
        out_h: int = 512,
        bands: list[int] | None = None,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 15.0,
        raw: bool = False,
        overview: str = "auto",
    ) -> None:
        """异步在后台线程向 Worker 请求分块读取，回调在 GUI 主线程执行。"""
        req_id = uuid.uuid4().hex
        with self._callbacks_lock:
            self._read_callbacks[req_id] = (on_success, on_error)

        def _runner() -> None:
            try:
                if not self.is_alive:
                    self.start()
                data = self._worker.read_window(
                    path=path,
                    x_off=x_off,
                    y_off=y_off,
                    win_w=win_w,
                    win_h=win_h,
                    out_w=out_w,
                    out_h=out_h,
                    bands=bands,
                    timeout=timeout,
                    raw=raw,
                    overview=overview,
                )
                self.sig_window_read.emit(data)
                self._sig_read_dispatch.emit(req_id, data, False)
            except Exception as exc:
                self.sig_error.emit("read_window_error", str(exc))
                self._sig_read_dispatch.emit(req_id, str(exc), True)

        self._spawn_thread(_runner, f"qt-read-{Path(path).stem}")

    # ------------------------------------------------------------------
    # 通用只读查询异步通道（P3-05）：坐标查询、行列反算、统计、概览
    # ------------------------------------------------------------------

    def _on_call_dispatched(self, req_id: str, payload: Any, is_error: bool) -> None:
        """在 GUI 主线程安全分发通用查询回调。"""
        with self._callbacks_lock:
            cb = self._call_callbacks.pop(req_id, None)
        if cb is None:
            return
        on_success, on_error = cb
        if is_error:
            if on_error:
                on_error(str(payload))
        elif on_success:
            on_success(payload)

    def call_async(
        self,
        fn: Callable[[], dict[str, Any]],
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> str:
        """在后台线程执行一次只读查询，回调固定在 GUI 主线程触发。

        调用方若只关心最新结果，应自行用代际号丢弃过期回调。
        """
        req_id = uuid.uuid4().hex
        with self._callbacks_lock:
            self._call_callbacks[req_id] = (on_success, on_error)

        def _runner() -> None:
            try:
                if not self.is_alive:
                    self.start()
                data = fn()
                self._sig_call_dispatch.emit(req_id, data, False)
            except Exception as exc:
                self._sig_call_dispatch.emit(req_id, f"{type(exc).__name__}: {exc}", True)

        self._spawn_thread(_runner, f"qt-call-{req_id[:6]}")
        return req_id

    def query_pixel_async(
        self,
        path: Path | str,
        row: int,
        col: int,
        bands: list[int] | None = None,
        view_crs_wkt: str | None = None,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 15.0,
    ) -> str:
        return self.call_async(
            lambda: self._worker.query_pixel(
                path, row=row, col=col, bands=bands, view_crs_wkt=view_crs_wkt, timeout=timeout
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def map_to_pixel_async(
        self,
        path: Path | str,
        x: float,
        y: float,
        view_crs_wkt: str | None = None,
        convention: str = "corner",
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 15.0,
    ) -> str:
        return self.call_async(
            lambda: self._worker.map_to_pixel(
                path, x=x, y=y, view_crs_wkt=view_crs_wkt, convention=convention, timeout=timeout
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def compute_stats_async(
        self,
        path: Path | str,
        mode: str = "sampled",
        bands: list[int] | None = None,
        max_samples: int = 1_000_000,
        full_limit: int | None = None,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> str:
        return self.call_async(
            lambda: self._worker.compute_stats(
                path, mode=mode, bands=bands, max_samples=max_samples,
                full_limit=full_limit, timeout=timeout,
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def dataset_overviews_async(
        self,
        path: Path | str,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 15.0,
    ) -> str:
        return self.call_async(
            lambda: self._worker.dataset_overviews(path, timeout=timeout),
            on_success=on_success,
            on_error=on_error,
        )

    def query_pixel(self, path: Path | str, row: int, col: int, **kw) -> dict[str, Any]:
        """同步版坐标查询（测试与脚本用）。"""
        if not self.is_alive:
            self.start()
        return self._worker.query_pixel(path, row=row, col=col, **kw)

    def map_to_pixel(self, path: Path | str, x: float, y: float, **kw) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.map_to_pixel(path, x=x, y=y, **kw)

    def compute_stats(self, path: Path | str, **kw) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.compute_stats(path, **kw)

    def dataset_overviews(self, path: Path | str) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.dataset_overviews(path)

    def crs_wkt(self, epsg: int) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.crs_wkt(epsg)

    def transform_point(self, x: float, y: float, src_wkt: str, dst_wkt: str) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.transform_point(x, y, src_wkt, dst_wkt)

    def crs_wkt_async(
        self,
        epsg: int,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> str:
        return self.call_async(
            lambda: self._worker.crs_wkt(epsg), on_success=on_success, on_error=on_error
        )

    def transform_bounds(self, path: Path | str, view_crs_wkt: str) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.transform_bounds(path, view_crs_wkt)

    def transform_coordinates(
        self,
        path: Path | str,
        points: list[tuple[float, float]] | list[list[float]],
        from_space: str,
        to_space: str,
        src_crs_wkt: str | None = None,
        dst_crs_wkt: str | None = None,
        convention: str = "corner",
    ) -> dict[str, Any]:
        if not self.is_alive:
            self.start()
        return self._worker.transform_coordinates(
            path=path,
            points=points,
            from_space=from_space,
            to_space=to_space,
            src_crs_wkt=src_crs_wkt,
            dst_crs_wkt=dst_crs_wkt,
            convention=convention,
        )

    def transform_coordinates_async(
        self,
        path: Path | str,
        points: list[tuple[float, float]] | list[list[float]],
        from_space: str,
        to_space: str,
        src_crs_wkt: str | None = None,
        dst_crs_wkt: str | None = None,
        convention: str = "corner",
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> str:
        return self.call_async(
            lambda: self._worker.transform_coordinates(
                path=path,
                points=points,
                from_space=from_space,
                to_space=to_space,
                src_crs_wkt=src_crs_wkt,
                dst_crs_wkt=dst_crs_wkt,
                convention=convention,
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def read_view_window(self, path: Path | str, **kw) -> dict[str, Any]:
        """同步请求重投影窗口（测试与脚本用）。"""
        if not self.is_alive:
            self.start()
        return self._worker.read_view_window(path, **kw)

    def read_view_window_async(
        self,
        path: Path | str,
        view_crs_wkt: str,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
        out_w: int = 512,
        out_h: int = 512,
        bands: list[int] | None = None,
        resampling: str = "near",
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> str:
        return self.call_async(
            lambda: self._worker.read_view_window(
                path,
                view_crs_wkt=view_crs_wkt,
                x_min=x_min,
                y_min=y_min,
                x_max=x_max,
                y_max=y_max,
                out_w=out_w,
                out_h=out_h,
                bands=bands,
                resampling=resampling,
                timeout=timeout,
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def transform_bounds_async(
        self,
        path: Path | str,
        view_crs_wkt: str,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> str:
        return self.call_async(
            lambda: self._worker.transform_bounds(path, view_crs_wkt),
            on_success=on_success,
            on_error=on_error,
        )

    def cancel_task(self, task_id: str | None = None) -> None:
        """取消当前或指定任务。"""
        tid = task_id or self._active_task_id
        self._worker.cancel_task(tid)

    def stop(self, timeout: float = 5.0) -> int:
        """停止 Worker 进程并释放资源。"""
        with self._callbacks_lock:
            for _, on_err in self._probe_callbacks.values():
                if on_err:
                    on_err("Worker client stopped")
            self._probe_callbacks.clear()
            for _, on_err in self._read_callbacks.values():
                if on_err:
                    on_err("Worker client stopped")
            self._read_callbacks.clear()
            for _, on_err in list(self._call_callbacks.values()):
                if on_err:
                    on_err("Worker client stopped")
            self._call_callbacks.clear()
        self._join_request_threads(timeout=timeout)
        code = self._worker.stop(timeout=timeout)
        self.sig_worker_stopped.emit(code)
        return code

    def kill(self) -> None:
        """强制杀死子进程。"""
        self._join_request_threads(timeout=1.0)
        with self._callbacks_lock:
            self._probe_callbacks.clear()
            self._read_callbacks.clear()
            self._call_callbacks.clear()
        self._worker.kill()
        self.sig_worker_stopped.emit(-9)
