"""Worker 独立子进程 CLI 入口（P1-02 / P1-03）。

运行于对应引擎环境（如 GDAL 运行时或 Rasterio venv）。
职责：
1. 建立与 UI/调度器的 UTF-8 JSONL 通信管道（标准输入输出）。
2. 执行握手协议（Kind.hello -> Kind.ready）。
3. 主线程持续读取标准输入，确保长耗时计算期间仍能即时响应 Kind.cancel 与 Kind.heartbeat。
4. 专用执行线程运行 ProviderAdapter.execute，报告 progress、log 与终态 result。
5. 保护 stdout 二进制流不被第三方 print 污染（print 重定向至 stderr）。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from spectra_workbench.domain.models import TaskSpec, TaskStatus
from spectra_workbench.protocol.messages import (
    Envelope,
    JsonlStreamParser,
    Kind,
    ProtocolError,
    encode_message,
    new_message,
)
from spectra_workbench.providers.base import (
    ExecutionContext,
    ProviderAdapter,
    TaskCancelledError,
    resolve_dataset_path,
)


def _create_adapter(provider_id: str) -> ProviderAdapter:
    """按 provider_id 动态加载对应引擎适配器。"""
    if provider_id == "gdal":
        from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter

        return GDALProviderAdapter()
    if provider_id == "rasterio":
        from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter

        return RasterioProviderAdapter()
    if provider_id == "dummy":
        return _create_dummy_adapter()
    raise ValueError(f"未知或未配置的 provider_id: {provider_id}")


def _create_dummy_adapter() -> ProviderAdapter:
    """供单元测试与协议联调使用的桩适配器（零原生依赖）。"""
    from spectra_workbench.domain.models import (
        Availability,
        Binding,
        CapabilityDescriptor,
        CapabilityEngineSpec,
        CapabilityKind,
        CapabilitySource,
        CapabilityState,
        DatasetKind,
        Entrypoint,
        PortSpec,
        Presence,
        TaskResult,
        Validation,
    )

    class DummyAdapter(ProviderAdapter):
        @property
        def provider_id(self) -> str:
            return "dummy"

        def runtime_info(self) -> dict[str, Any]:
            return {"available": True, "provider_id": "dummy", "version": "1.0.0"}

        def discover(self) -> list[CapabilityDescriptor]:
            return [
                CapabilityDescriptor(
                    capability_id="dummy:noop",
                    descriptor_revision="1.0",
                    kind=CapabilityKind.algorithm,
                    name="Dummy No-op",
                    native_name="dummy",
                    category=["test"],
                    description="Test no-op",
                    engine=CapabilityEngineSpec(provider_id="dummy", version_constraint=">=1.0"),
                    source=CapabilitySource(documentation_url="https://example.com", baseline_id="dummy", upstream_version="1.0"),
                    inputs=[PortSpec(key="input", data_type=DatasetKind.scalar, required=False)],
                    outputs=[PortSpec(key="output", data_type=DatasetKind.scalar, required=True)],
                    state=CapabilityState(
                        presence=Presence.present,
                        binding=Binding.form,
                        validation=Validation.smoke_passed,
                        availability=Availability.ready,
                    ),
                    entrypoints=[Entrypoint(kind="api", target="dummy", available=True)],
                )
            ]

        def validate(self, spec: TaskSpec) -> dict[str, Any]:
            return {"valid": True, "errors": []}

        def estimate(self, spec: TaskSpec) -> dict[str, Any]:
            return {"estimated_ram_mib": 32, "estimated_disk_bytes": 1024}

        def execute(self, spec: TaskSpec, context: ExecutionContext) -> TaskResult:
            context.log("INFO", "Dummy task starting")
            uncooperative = bool(spec.parameters.get("uncooperative", False))
            steps = spec.parameters.get("steps", 3)
            delay = spec.parameters.get("delay", 0.05)
            for i in range(steps):
                if not uncooperative:
                    context.check_cancelled()
                time.sleep(delay)
                ratio = (i + 1) / steps
                context.progress(ratio, "working", f"Step {i + 1}/{steps}")
            if not uncooperative:
                context.check_cancelled()
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.succeeded,
                outputs=[{"key": "output", "uri": "memory://dummy", "data_type": "scalar"}],
                duration_seconds=0.1,
            )

        def probe_dataset(self, path: Path | str) -> dict[str, Any]:
            return {
                "path": str(path),
                "driver": "DummyRaster",
                "driver_long_name": "Dummy Synthetic Raster",
                "width": 16,
                "height": 16,
                "count": 1,
                "dtypes": ["Float32"],
                "nodata": None,
                "crs_epsg": 4326,
                "crs_wkt": None,
                "geotransform": [0.0, 1.0, 0.0, 16.0, 0.0, -1.0],
                "bounds": [0.0, 0.0, 16.0, 16.0],
                "bands": [{"band": 1, "dtype": "Float32", "nodata": None, "color_interp": "Gray", "overviews": 0}],
            }

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
            raw: bool = False,
            overview: str = "auto",
        ) -> dict[str, Any]:
            import base64
            buf = bytearray(out_w * out_h)
            for y in range(out_h):
                for x in range(out_w):
                    buf[y * out_w + x] = (x + y) % 256
            b64_str = base64.b64encode(bytes(buf)).decode("ascii")
            return {
                "x_off": x_off,
                "y_off": y_off,
                "win_w": win_w,
                "win_h": win_h,
                "out_w": out_w,
                "out_h": out_h,
                "is_rgb": False,
                "bands": [1],
                "b64_data": b64_str,
                "stats": [{"band": 1, "min": 0.0, "max": 255.0}],
            }

        def transform_coordinates(
            self,
            path: Path | str,
            points: list[list[float]] | list[tuple[float, float]],
            from_space: str,
            to_space: str,
            src_crs_wkt: str | None = None,
            dst_crs_wkt: str | None = None,
            convention: str = "corner",
        ) -> dict[str, Any]:
            return {"points": [list(p) for p in points], "from_space": from_space, "to_space": to_space}

    return DummyAdapter()


class WorkerRuntime:
    """Worker 运行实例。"""

    def __init__(self, provider_id: str, worker_id: str, generation: int = 0):
        self.provider_id = provider_id
        self.worker_id = worker_id
        self.generation = generation
        self.adapter = _create_adapter(provider_id)

        assert sys.__stdout__ is not None and hasattr(sys.__stdout__, "buffer")
        self._real_stdout = sys.__stdout__.buffer
        # 将标准 print 导向 stderr，防止破坏 stdout JSONL 协议流
        sys.stdout = sys.stderr

        self._write_lock = threading.Lock()
        self._task_queue: queue.Queue[Envelope | None] = queue.Queue()
        self._active_context: ExecutionContext | None = None
        self._active_task_id: str | None = None
        self._stop_event = threading.Event()

        # 大控制消息落盘溢出目录：每个 worker 进程独占一个子目录。
        # 客户端消费时会删除条目；进程退出时再清扫本进程残留，
        # 因此成功/失败/取消/崩溃退出都不会留下 spill 垃圾。
        self.spill_dir: Path | None = None
        try:
            spill_root = Path(tempfile.gettempdir()) / "spectra_workbench_spill"
            spill_root.mkdir(parents=True, exist_ok=True)
            self._spill_root = spill_root
            mine = spill_root / f"worker-{os.getpid()}-{int(time.time())}"
            mine.mkdir(parents=True, exist_ok=True)
            self.spill_dir = mine
            self._sweep_stale_spill(spill_root)
        except Exception:
            self.spill_dir = None

    @staticmethod
    def _sweep_stale_spill(spill_root: Path, max_age_seconds: float = 900.0) -> int:
        """删除明显陈旧的 worker spill 目录（崩溃遗留），不动其它进程正在用的目录。"""
        removed = 0
        try:
            now = time.time()
            for child in spill_root.iterdir():
                if not child.name.startswith("worker-"):
                    continue
                try:
                    if now - child.stat().st_mtime > max_age_seconds:
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            return removed
        return removed

    def _cleanup_own_spill(self) -> int:
        """退出时清掉本进程产生的 spill 文件（覆盖失败/取消/未消费的情况）。"""
        target = getattr(self, "spill_dir", None)
        if target is None:
            return 0
        removed = 0
        try:
            for f in target.glob("*.json"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass
        return removed

    def send_envelope(self, env: Envelope) -> None:
        """线程安全地向标准输出写入一行编码后的 JSONL 消息。"""
        data = encode_message(env, spill_dir=self.spill_dir)
        with self._write_lock:
            self._real_stdout.write(data)
            self._real_stdout.flush()

    def send_error(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """按 Schema 规范构造并发送合法 Kind.error 响应（必须包含 retryable）。"""
        payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
        if details is not None:
            payload["details"] = details
        self.send_kind(
            Kind.error,
            payload,
            request_id=request_id,
            task_id=task_id,
        )

    def send_kind(
        self,
        kind: Kind,
        payload: dict[str, Any],
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        env = new_message(
            kind=kind,
            worker_id=self.worker_id,
            payload=payload,
            request_id=request_id,
            task_id=task_id,
            generation=self.generation,
        )
        self.send_envelope(env)

    def start(self) -> None:
        """启动 Worker 执行线程与握手序列。"""
        # 1. 握手序列
        self.send_kind(
            Kind.hello,
            {
                "provider_id": self.provider_id,
                "worker_package_version": "0.0.1",
            },
        )
        self.send_kind(
            Kind.ready,
            {
                "runtime_id": f"{self.provider_id}-worker",
                "features": ["execute_task", "cooperative_cancel", "heartbeat"],
            },
        )

        # 2. 启动后台任务执行线程
        executor_thread = threading.Thread(target=self._executor_loop, name="worker-executor", daemon=True)
        executor_thread.start()

        # 3. 主线程持续读取标准输入分帧
        parser = JsonlStreamParser()
        stdin_buf = sys.stdin.buffer

        try:
            while not self._stop_event.is_set():
                chunk = stdin_buf.readline()
                if not chunk:
                    # 管道关闭（EOF），父进程退出或断连
                    break
                try:
                    envelopes = parser.feed(chunk)
                except ProtocolError as p_err:
                    self.send_kind(
                        Kind.error,
                        {
                            "code": "protocol_error",
                            "message": str(p_err),
                            "retryable": False,
                            "details": p_err.to_payload(),
                        },
                    )
                    continue

                for env in envelopes:
                    self._dispatch_envelope(env)
        finally:
            self._stop_event.set()
            self._cleanup_own_spill()
            if self._active_context:
                self._active_context.cancel()
            self._task_queue.put(None)
            executor_thread.join(timeout=2.0)

    def _dispatch_envelope(self, env: Envelope) -> None:
        """主线程快速分发控制消息与入队任务。"""
        if env.kind is Kind.heartbeat:
            self.send_kind(
                Kind.heartbeat,
                {
                    "monotonic_seconds": round(time.monotonic(), 3),
                    "active_task_id": self._active_task_id,
                },
                request_id=env.request_id,
                task_id=self._active_task_id,
            )
            return

        if env.kind is Kind.cancel:
            if self._active_context is not None and (env.task_id is None or env.task_id == self._active_context.task_id):
                self._active_context.cancel()
            return

        if env.kind is Kind.request:
            action = env.payload.get("action")
            if action == "shutdown":
                self._stop_event.set()
                self._task_queue.put(None)
                return
            if action == "ping":
                self.send_kind(
                    Kind.heartbeat,
                    {
                        "monotonic_seconds": round(time.monotonic(), 3),
                        "active_task_id": self._active_task_id,
                    },
                    request_id=env.request_id,
                    task_id=self._active_task_id,
                )
                return
            # 其他请求（如 execute_task）交由执行线程排队
            self._task_queue.put(env)
            return

        # 其他未预期消息
        self.send_kind(
            Kind.error,
            {
                "code": "unexpected_message",
                "message": f"Worker does not handle incoming {env.kind.value} message",
                "retryable": False,
            },
            request_id=env.request_id,
            task_id=env.task_id,
        )

    def _executor_loop(self) -> None:
        """执行线程主循环：顺序执行任务，不阻塞控制通道。

        通过顶层异常隔离保护，防止单次请求异常或响应序列化失败导致执行线程非正常退出。
        """
        while not self._stop_event.is_set():
            try:
                env = self._task_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if env is None:
                break

            action = env.payload.get("action")
            try:
                if action == "execute_task":
                    self._handle_execute_task(env)
                elif action == "probe_dataset":
                    self._handle_probe_dataset(env)
                elif action == "read_window":
                    self._handle_read_window(env)
                elif action in self._SIMPLE_CALLS:
                    self._handle_simple_call(env, action)
                else:
                    self.send_error(
                        code="unknown_action",
                        message=f"Unknown action: {action}",
                        retryable=False,
                        request_id=env.request_id,
                        task_id=env.task_id,
                    )
            except Exception as loop_exc:
                # 顶层异常隔离：即使请求处理本身抛出未捕获异常，执行线程仍保持健康并向调用方返回合法错误
                with contextlib.suppress(Exception):
                    self.send_error(
                        code="executor_internal_error",
                        message=f"执行请求内部异常 ({action}): {loop_exc}",
                        retryable=False,
                        request_id=env.request_id,
                        task_id=env.task_id,
                    )

    # action -> (adapter 方法名, 允许透传的参数名, 是否需要 path)
    _SIMPLE_CALLS: dict[str, tuple[str, tuple[str, ...], bool]] = {
        "query_pixel": ("query_pixel", ("row", "col", "bands", "view_crs_wkt"), True),
        "map_to_pixel": ("map_to_pixel", ("x", "y", "view_crs_wkt", "convention"), True),
        "compute_stats": ("compute_stats", ("mode", "bands", "max_samples", "full_limit"), True),
        "dataset_overviews": ("dataset_overviews", (), True),
        "transform_bounds": ("transform_bounds", ("view_crs_wkt",), True),
        "read_view_window": (
            "read_view_window",
            ("view_crs_wkt", "x_min", "y_min", "x_max", "y_max", "out_w", "out_h", "bands", "resampling"),
            True,
        ),
        "crs_wkt": ("crs_wkt", ("epsg",), False),
        "transform_point": ("transform_point", ("x", "y", "src_wkt", "dst_wkt", "z"), False),
        "transform_coordinates": (
            "transform_coordinates",
            ("points", "from_space", "to_space", "src_crs_wkt", "dst_crs_wkt", "convention"),
            True,
        ),
    }

    def _handle_simple_call(self, env: Envelope, action: str) -> None:
        """把只读查询类请求直接透传给 adapter，并按统一 Schema 回结果或错误。"""
        method_name, allowed, needs_path = self._SIMPLE_CALLS[action]
        args = env.payload.get("arguments", {}) or {}
        path_arg = args.get("path")
        path_str: str | None = path_arg if isinstance(path_arg, str) else None
        if needs_path and path_str is None:
            self.send_error(
                code="invalid_arguments",
                message=f"Missing 'path' argument in {action}",
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return
        method = getattr(self.adapter, method_name, None)
        if method is None:
            self.send_error(
                code="capability_not_implemented",
                message=f"provider '{getattr(self.adapter, 'provider_id', '?')}' 未实现 {action}",
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return
        kwargs: dict[str, Any] = {}
        for key in allowed:
            if key in args:
                kwargs[key] = args[key]
        try:
            if needs_path and path_str is not None:
                resolved = resolve_dataset_path(path_str, getattr(self, "project_root", Path.cwd()))
                data = method(resolved, **kwargs)
            else:
                data = method(**kwargs)
            self.send_kind(
                Kind.result,
                {"status": "succeeded", "artifact_ids": [], "action": action, "data": data},
                request_id=env.request_id,
                task_id=env.task_id,
            )
        except Exception as exc:
            self.send_error(
                code=f"{action}_failed",
                message=str(exc),
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )

    def _handle_probe_dataset(self, env: Envelope) -> None:
        args = env.payload.get("arguments", {})
        path_str = args.get("path")
        if not path_str:
            self.send_error(
                code="invalid_arguments",
                message="Missing 'path' argument in probe_dataset",
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return
        try:
            resolved = resolve_dataset_path(path_str, getattr(self, "project_root", Path.cwd()))
            info = self.adapter.probe_dataset(resolved)
            self.send_kind(
                Kind.result,
                {
                    "status": "succeeded",
                    "artifact_ids": [],
                    "action": "probe_dataset",
                    "data": info,
                },
                request_id=env.request_id,
                task_id=env.task_id,
            )
        except Exception as exc:
            self.send_error(
                code="probe_failed",
                message=str(exc),
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )

    def _handle_read_window(self, env: Envelope) -> None:
        args = env.payload.get("arguments", {})
        path_str = args.get("path")
        if not path_str:
            self.send_error(
                code="invalid_arguments",
                message="Missing 'path' argument in read_window",
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return
        try:
            resolved = resolve_dataset_path(path_str, getattr(self, "project_root", Path.cwd()))
            x_off = int(args.get("x_off", 0))
            y_off = int(args.get("y_off", 0))
            win_w = int(args.get("win_w", 512))
            win_h = int(args.get("win_h", 512))
            out_w = int(args.get("out_w", 512))
            out_h = int(args.get("out_h", 512))
            raw_bands = args.get("bands")
            bands: list[int] | None = None
            if raw_bands is not None:
                if isinstance(raw_bands, list):
                    bands = [int(b) for b in raw_bands]
                else:
                    bands = [int(raw_bands)]

            data = self.adapter.read_window(
                resolved,
                x_off=x_off,
                y_off=y_off,
                win_w=win_w,
                win_h=win_h,
                out_w=out_w,
                out_h=out_h,
                bands=bands,
                raw=bool(args.get("raw", False)),
                overview=str(args.get("overview", "auto")),
            )
            self.send_kind(
                Kind.result,
                {
                    "status": "succeeded",
                    "artifact_ids": [],
                    "action": "read_window",
                    "data": data,
                },
                request_id=env.request_id,
                task_id=env.task_id,
            )
        except Exception as exc:
            self.send_error(
                code="read_window_failed",
                message=str(exc),
                retryable=False,
                request_id=env.request_id,
                task_id=env.task_id,
            )

    def _handle_execute_task(self, env: Envelope) -> None:
        args = env.payload.get("arguments", {})
        spec_dict = args.get("spec")
        if not isinstance(spec_dict, dict):
            self.send_kind(
                Kind.error,
                {
                    "code": "invalid_task_spec",
                    "message": "execute_task requires 'spec' dictionary in arguments",
                    "retryable": False,
                },
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return

        try:
            spec = TaskSpec.model_validate(spec_dict)
        except Exception as exc:
            self.send_kind(
                Kind.error,
                {
                    "code": "spec_validation_failed",
                    "message": f"TaskSpec validation failed: {exc}",
                    "retryable": False,
                },
                request_id=env.request_id,
                task_id=env.task_id,
            )
            return

        self._active_task_id = spec.task_id

        # 适配器准入校验：检查能力路由与输入参数
        val = self.adapter.validate(spec)
        if not val.get("valid", True):
            err_code = val.get("code", "validation_failed")
            err_msg = "; ".join(val.get("errors", ["Adapter validation failed"]))
            from spectra_workbench.domain.models import TaskResult

            fail_res = TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": err_code, "message": err_msg},
                duration_seconds=0.0,
            )
            self.send_kind(
                Kind.result,
                {
                    "status": fail_res.status.value,
                    "artifact_ids": [f"task-result-{spec.task_id}"],
                    "task_result": fail_res.to_contract_dict(),
                },
                request_id=env.request_id,
                task_id=spec.task_id,
            )
            self._active_task_id = None
            return

        # 发送 accepted 确认
        self.send_kind(
            Kind.accepted,
            {"attempt": spec.attempt},
            request_id=env.request_id,
            task_id=spec.task_id,
        )

        from spectra_workbench.providers.base import resolve_dataset_path

        workspace = resolve_dataset_path(spec.workspace, spec.project_root)
        staging_dir = workspace / ".staging" / spec.task_id

        def _on_progress(ratio: float | None, phase: str, msg: str | None) -> None:
            p_load: dict[str, Any] = {"ratio": ratio, "phase": phase}
            if msg is not None:
                p_load["message"] = msg
            self.send_kind(Kind.progress, p_load, request_id=env.request_id, task_id=spec.task_id)

        def _on_log(level: str, msg: str, stream: str | None) -> None:
            l_load: dict[str, Any] = {"level": level, "message": msg}
            if stream is not None:
                l_load["stream"] = stream
            self.send_kind(Kind.log, l_load, request_id=env.request_id, task_id=spec.task_id)

        ctx = ExecutionContext(
            task_id=spec.task_id,
            workspace=workspace,
            staging_dir=staging_dir,
            progress_callback=_on_progress,
            log_callback=_on_log,
            configuration=spec.configuration,
        )
        self._active_context = ctx

        try:
            result = self.adapter.execute(spec, ctx)
        except TaskCancelledError:
            from spectra_workbench.domain.models import TaskResult

            result = TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": f"Task {spec.task_id} was cancelled cooperatively"},
            )
        except Exception as exc:
            from spectra_workbench.domain.models import TaskResult

            result = TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "execution_uncaught_exception", "message": str(exc)},
            )
        finally:
            self._active_context = None
            self._active_task_id = None

        # 发送 result 终态
        art_ids = [o["uri"] for o in result.outputs] if result.outputs else [f"task-result-{spec.task_id}"]
        res_payload: dict[str, Any] = {
            "status": result.status.value,
            "artifact_ids": art_ids,
            "task_result": result.to_contract_dict(),
        }
        if result.warnings:
            res_payload["warnings"] = result.warnings
        self.send_kind(Kind.result, res_payload, request_id=env.request_id, task_id=spec.task_id)

        # 任务结束后重新上报就绪状态
        self.send_kind(
            Kind.ready,
            {
                "runtime_id": f"{self.provider_id}-worker",
                "features": ["execute_task", "cooperative_cancel", "heartbeat"],
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectra Workbench Engine Worker CLI")
    parser.add_argument("provider_id", help="Engine provider identifier (e.g. gdal, rasterio, science, dummy)")
    parser.add_argument("--worker-id", default=None, help="Explicit worker ID string")
    parser.add_argument("--generation", type=int, default=0, help="Process generation number")

    args = parser.parse_args()
    worker_id = args.worker_id or f"sw-{args.provider_id}-{uuid.uuid4().hex[:6]}"

    runtime = WorkerRuntime(
        provider_id=args.provider_id,
        worker_id=worker_id,
        generation=args.generation,
    )
    runtime.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
