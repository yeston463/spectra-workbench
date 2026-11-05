"""Worker 进程生命周期与 JSONL 通信客户端（P1-02）。

在 UI / 调度器进程侧运行（零原生地理依赖）。
职责：
1. 启动指定引擎子进程（按 provider_id 注入独立 Python 解释器与环境变量）。
2. 在后台线程异步分帧读取 stdout（UTF-8 JSONL），防止 UI / 主线程阻塞。
3. 独立后台线程捕获 stderr，防止子进程缓冲区打满死锁，且不污染 JSONL 协议流。
4. 校验握手序列（hello -> ready）、世代号守卫（GenerationGuard）与请求超时机制。
5. 提供任务执行、协作取消、心跳保活与优雅退出接口。
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import TaskResult, TaskSpec, TaskStatus
from spectra_workbench.protocol.messages import (
    Envelope,
    GenerationGuard,
    JsonlStreamParser,
    Kind,
    encode_message,
    new_message,
)


class WorkerProcessError(Exception):
    """Worker 进程异常。"""


class WorkerTimeoutError(WorkerProcessError):
    """Worker 响应超时。"""


class WorkerProtocolViolationError(WorkerProcessError):
    """Worker 违反协议契约。"""


def resolve_worker_command(
    provider_id: str,
    project_root: Path | str | None = None,
) -> tuple[Path, dict[str, str]]:
    """根据 provider_id 解析对应子进程解释器绝对路径与专用环境变量。"""
    root = find_project_root(project_root)
    base_env = os.environ.copy()
    base_env["PYTHONIOENCODING"] = "utf-8"

    # worker 必须导入 GUI 进程正在使用的同一份代码：从已加载模块定位，而不是猜 project_root
    src_dir = str(Path(__file__).resolve().parents[2])
    existing_pp = base_env.get("PYTHONPATH", "")
    base_env["PYTHONPATH"] = f"{src_dir};{existing_pp}" if existing_pp else src_dir

    if provider_id == "gdal":
        gdal_info = resolve_gdal_runtime(root)
        py_path = Path(gdal_info["python"])
        base_env.update(gdal_info["env"])
        return py_path, base_env

    if provider_id == "rasterio":
        py_path = root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe"
        if not py_path.exists():
            py_path = Path(sys.executable)
        return py_path, base_env

    if provider_id == "science":
        py_path = root / ".venvs" / "sw-science" / "Scripts" / "python.exe"
        if not py_path.exists():
            py_path = Path(sys.executable)
        return py_path, base_env

    if provider_id in {"dummy", "test"}:
        return Path(sys.executable), base_env

    raise ValueError(f"未知 provider_id: {provider_id}")


def _resolve_file_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(url2pathname(unquote(parsed.path)))
    return Path(uri)


class WorkerProcess:
    """跨进程 Worker 客户端。"""

    def __init__(
        self,
        provider_id: str,
        worker_id: str | None = None,
        generation: int = 0,
        project_root: Path | str | None = None,
        custom_python: Path | str | None = None,
        custom_env: dict[str, str] | None = None,
    ):
        self.provider_id = provider_id
        self.worker_id = worker_id or f"sw-{provider_id}-{uuid.uuid4().hex[:6]}"
        self.generation = generation
        self.project_root = find_project_root(project_root)

        self._guard = GenerationGuard(current_generation=generation)
        self._proc: subprocess.Popen[bytes] | None = None

        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.stderr_lines: list[str] = []
        self.received_envelopes: list[Envelope] = []
        self._listeners: list[Callable[[Envelope], None]] = []

        self._response_waiters: dict[str, queue.Queue[Envelope]] = {}
        self._waiters_lock = threading.Lock()

        self._hello_envelope: Envelope | None = None
        self._ready_envelope: Envelope | None = None

        # 配置大控制消息落盘溢出目录
        try:
            spill_root = Path(tempfile.gettempdir()) / "spectra_workbench_spill"
            spill_root.mkdir(parents=True, exist_ok=True)
            self.spill_dir: Path | None = spill_root
        except Exception:
            self.spill_dir = None

        # 解析命令与环境
        if custom_python:
            self._python_exe = Path(custom_python)
            self._env = custom_env or os.environ.copy()
        else:
            self._python_exe, self._env = resolve_worker_command(provider_id, self.project_root)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def add_listener(self, listener: Callable[[Envelope], None]) -> None:
        """添加消息监听器（每收到一条有效协议消息均回调）。"""
        self._listeners.append(listener)

    def start(self, timeout: float = 15.0) -> Envelope:
        """启动 Worker 进程并阻塞等待握手完成（hello -> ready）。"""
        cmd = [
            str(self._python_exe),
            "-m",
            "spectra_workbench.workers.entrypoint",
            self.provider_id,
            "--worker-id",
            self.worker_id,
            "--generation",
            str(self.generation),
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                bufsize=0,
            )
        except Exception as exc:
            raise WorkerProcessError(f"启动 Worker 进程失败: {exc} (cmd={cmd})") from exc

        # 启动 stdout 与 stderr 读取线程
        self._stop_event.clear()
        self._stdout_thread = threading.Thread(
            target=self._read_stdout_loop,
            name=f"worker-{self.worker_id}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_loop,
            name=f"worker-{self.worker_id}-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # 等待 ready 消息
        start_t = time.monotonic()
        while time.monotonic() - start_t < timeout:
            if not self.is_alive:
                stderr_preview = "".join(self.stderr_lines[-20:])
                raise WorkerProcessError(
                    f"Worker 进程在握手期间异常退出 (code={self._proc.poll()}):\n{stderr_preview}"
                )
            if self._ready_envelope is not None:
                return self._ready_envelope
            time.sleep(0.05)

        self.kill()
        raise WorkerTimeoutError(f"Worker 启动握手超时 ({timeout}s)")

    def _read_stdout_loop(self) -> None:
        """后台分帧读取 stdout 二进制流。"""
        parser = JsonlStreamParser()
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        while not self._stop_event.is_set():
            try:
                line = stdout.readline()
                if not line:
                    break
                envelopes = parser.feed(line)
                for env in envelopes:
                    if not self._guard.accept(env):
                        continue
                    self._on_envelope_received(env)
            except Exception as exc:
                self.stderr_lines.append(f"[Client Protocol Error] {exc}\n")

        # 刷新最后遗留
        try:
            for env in parser.close():
                if self._guard.accept(env):
                    self._on_envelope_received(env)
        except Exception:
            pass

    def _read_stderr_loop(self) -> None:
        """后台读取 stderr 并记录，防止管道填满造成子进程死锁。"""
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while not self._stop_event.is_set():
            try:
                line = stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                self.stderr_lines.append(text)
            except Exception:
                break

    def _on_envelope_received(self, env: Envelope) -> None:
        """接收并分发协议消息。若收到落盘溢出的 artifact 消息，自动解包还原。"""
        if env.kind is Kind.artifact and env.payload.get("kind") == "inline_payload_spill":
            uri = env.payload.get("uri")
            if uri:
                try:
                    spill_path = _resolve_file_uri(uri)
                    if spill_path.exists():
                        raw_data = spill_path.read_text(encoding="utf-8")
                        payload_dict = json.loads(raw_data)
                        env = Envelope.model_validate(payload_dict)
                        spill_path.unlink(missing_ok=True)
                except Exception:
                    pass

        # 有界容量保留历史消息（最多保留 200 条），防止长期运行内存无界膨胀
        self.received_envelopes.append(env)
        if len(self.received_envelopes) > 200:
            self.received_envelopes = self.received_envelopes[-200:]

        if env.kind is Kind.hello:
            self._hello_envelope = env
        elif env.kind is Kind.ready and self._ready_envelope is None:
            self._ready_envelope = env

        # 唤醒等待该 request_id 的调用方
        if env.request_id:
            with self._waiters_lock:
                q = self._response_waiters.get(env.request_id)
                if q is not None:
                    q.put(env)

        # 触发监听器
        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(env)

    def send_envelope(self, env: Envelope) -> None:
        """向 Worker 写入一行 JSONL。"""
        if not self.is_alive or self._proc is None or self._proc.stdin is None:
            raise WorkerProcessError("Worker 进程已终止，无法发送消息")
        data = encode_message(env, spill_dir=self.spill_dir)
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except OSError as exc:
            raise WorkerProcessError(f"向 Worker 写入管道失败: {exc}") from exc

    def send_request(
        self,
        action: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        timeout: float = 10.0,
    ) -> Envelope:
        """发送 Kind.request 并阻塞等待第一个对应响应（如 accepted / error）。"""
        req_id = uuid.uuid4().hex
        q: queue.Queue[Envelope] = queue.Queue()
        with self._waiters_lock:
            self._response_waiters[req_id] = q

        try:
            env = new_message(
                Kind.request,
                self.worker_id,
                {"action": action, "arguments": arguments},
                request_id=req_id,
                task_id=task_id,
                generation=self.generation,
            )
            self.send_envelope(env)

            resp = q.get(timeout=timeout)
            return resp
        except queue.Empty as err:
            raise WorkerTimeoutError(f"请求 {action} 超时 ({timeout}s)") from err
        finally:
            with self._waiters_lock:
                self._response_waiters.pop(req_id, None)

    def execute_task(
        self,
        spec: TaskSpec,
        timeout: float = 60.0,
        on_progress: Callable[[Envelope], None] | None = None,
        on_log: Callable[[Envelope], None] | None = None,
    ) -> TaskResult:
        """提交并完整执行一项任务，流式接收 progress / log 直至 result 终态。"""
        req_id = uuid.uuid4().hex
        resp_q: queue.Queue[Envelope] = queue.Queue()
        with self._waiters_lock:
            self._response_waiters[req_id] = resp_q

        # 发送执行请求
        req_env = new_message(
            Kind.request,
            self.worker_id,
            {"action": "execute_task", "arguments": {"spec": spec.to_contract_dict()}},
            request_id=req_id,
            task_id=spec.task_id,
            generation=self.generation,
        )
        self.send_envelope(req_env)

        start_t = time.monotonic()

        try:
            while time.monotonic() - start_t < timeout:
                if not self.is_alive:
                    stderr_msg = "".join(self.stderr_lines[-20:])
                    raise WorkerProcessError(f"Worker 进程在任务执行期间崩溃退出:\n{stderr_msg}")

                try:
                    env = resp_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if env.kind is Kind.accepted:
                    continue
                elif env.kind is Kind.progress:
                    if on_progress:
                        on_progress(env)
                elif env.kind is Kind.log:
                    if on_log:
                        on_log(env)
                elif env.kind is Kind.result:
                    raw_res = env.payload.get("task_result")
                    if isinstance(raw_res, dict):
                        return TaskResult.model_validate(raw_res)
                    # 容底构造 TaskResult
                    status_str = env.payload.get("status", "failed")
                    status_enum = TaskStatus(status_str)
                    return TaskResult(
                        task_id=spec.task_id,
                        attempt=spec.attempt,
                        status=status_enum,
                        outputs=[{"uri": art, "data_type": "artifact"} for art in env.payload.get("artifact_ids", [])],
                        warnings=env.payload.get("warnings", []),
                    )
                elif env.kind is Kind.error:
                    msg = env.payload.get("message", "Unknown worker error")
                    code = env.payload.get("code", "worker_error")
                    return TaskResult(
                        task_id=spec.task_id,
                        attempt=spec.attempt,
                        status=TaskStatus.failed,
                        error={"code": code, "message": msg},
                    )

            raise WorkerTimeoutError(f"任务 {spec.task_id} 执行超时 ({timeout}s)")
        finally:
            with self._waiters_lock:
                self._response_waiters.pop(req_id, None)

    def cancel_task(self, task_id: str | None = None) -> None:
        """向 Worker 发送协作式取消请求。"""
        env = new_message(
            Kind.cancel,
            self.worker_id,
            {"reason": "Cancelled by user"},
            task_id=task_id,
            generation=self.generation,
        )
        self.send_envelope(env)

    def heartbeat(self, timeout: float = 5.0) -> Envelope:
        """发送心跳探测并等待返回。"""
        req_id = uuid.uuid4().hex
        q: queue.Queue[Envelope] = queue.Queue()
        with self._waiters_lock:
            self._response_waiters[req_id] = q

        try:
            env = new_message(
                Kind.heartbeat,
                self.worker_id,
                {"monotonic_seconds": round(time.monotonic(), 3), "active_task_id": None},
                request_id=req_id,
                generation=self.generation,
            )
            self.send_envelope(env)
            return q.get(timeout=timeout)
        except queue.Empty as err:
            raise WorkerTimeoutError(f"Worker 心跳响应超时 ({timeout}s)") from err
        finally:
            with self._waiters_lock:
                self._response_waiters.pop(req_id, None)

    def probe_dataset(self, path: Path | str, timeout: float = 10.0) -> dict[str, Any]:
        """向 Worker 发送探测请求并返回元数据字典。"""
        env = self.send_request("probe_dataset", {"path": str(path)}, timeout=timeout)
        if env.kind is Kind.error:
            raise RuntimeError(f"探测数据集失败: {env.payload.get('message')}")
        data = env.payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"无效探测响应: {env.payload}")
        return data

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
        """向 Worker 发送分块读取窗口请求并返回 base64 数据。"""
        args: dict[str, Any] = {
            "path": str(path),
            "x_off": x_off,
            "y_off": y_off,
            "win_w": win_w,
            "win_h": win_h,
            "out_w": out_w,
            "out_h": out_h,
        }
        if bands is not None:
            args["bands"] = bands
        if raw:
            args["raw"] = True
            args["overview"] = overview
        env = self.send_request("read_window", args, timeout=timeout)
        if env.kind is Kind.error:
            raise RuntimeError(f"分块读取窗口失败: {env.payload.get('message')}")
        data = env.payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"无效分块读取响应: {env.payload}")
        return data

    def _simple_call(
        self, action: str, args: dict[str, Any], timeout: float = 15.0
    ) -> dict[str, Any]:
        """只读查询类请求的统一收发。"""
        env = self.send_request(action, args, timeout=timeout)
        if env.kind is Kind.error:
            raise RuntimeError(f"{action} 失败: {env.payload.get('message')}")
        data = env.payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"无效 {action} 响应: {env.payload}")
        return data

    def query_pixel(
        self,
        path: Path | str,
        row: int,
        col: int,
        bands: list[int] | None = None,
        view_crs_wkt: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """像元行列 -> 原值/物理值/有效性 + 地图坐标（在 worker 内计算）。"""
        args: dict[str, Any] = {"path": str(path), "row": int(row), "col": int(col)}
        if bands is not None:
            args["bands"] = bands
        if view_crs_wkt:
            args["view_crs_wkt"] = view_crs_wkt
        return self._simple_call("query_pixel", args, timeout=timeout)

    def map_to_pixel(
        self,
        path: Path | str,
        x: float,
        y: float,
        view_crs_wkt: str | None = None,
        convention: str = "center",
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """地图坐标 -> 行列号（同时给出中心/角点两种约定）。"""
        args: dict[str, Any] = {"path": str(path), "x": float(x), "y": float(y), "convention": convention}
        if view_crs_wkt:
            args["view_crs_wkt"] = view_crs_wkt
        return self._simple_call("map_to_pixel", args, timeout=timeout)

    def compute_stats(
        self,
        path: Path | str,
        mode: str = "sampled",
        bands: list[int] | None = None,
        max_samples: int = 1_000_000,
        full_limit: int | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """统计（sampled 明确标注抽样；full 超上限会被拒绝，不偷偷降级）。"""
        args: dict[str, Any] = {"path": str(path), "mode": mode, "max_samples": int(max_samples)}
        if full_limit is not None:
            args["full_limit"] = int(full_limit)
        if bands is not None:
            args["bands"] = bands
        return self._simple_call("compute_stats", args, timeout=timeout)

    def dataset_overviews(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        return self._simple_call("dataset_overviews", {"path": str(path)}, timeout=timeout)

    def crs_wkt(self, epsg: int, timeout: float = 10.0) -> dict[str, Any]:
        """向 worker 索取 EPSG 对应的 WKT（GUI 侧不导入 osr）。"""
        return self._simple_call("crs_wkt", {"epsg": int(epsg)}, timeout=timeout)

    def transform_point(
        self, x: float, y: float, src_wkt: str, dst_wkt: str, timeout: float = 10.0
    ) -> dict[str, Any]:
        return self._simple_call(
            "transform_point",
            {"x": float(x), "y": float(y), "src_wkt": src_wkt, "dst_wkt": dst_wkt},
            timeout=timeout,
        )

    def transform_bounds(self, path: Path | str, view_crs_wkt: str, timeout: float = 15.0) -> dict[str, Any]:
        """数据集范围换算到视图 CRS。"""
        return self._simple_call(
            "transform_bounds", {"path": str(path), "view_crs_wkt": view_crs_wkt}, timeout=timeout
        )

    def probe_vector(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        """探测矢量数据集的基础元数据（P3-02）。"""
        return self._simple_call("probe_vector", {"path": str(path)}, timeout=timeout)

    def list_subdatasets(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        """列出容器数据集的子数据集（P3-02）。"""
        return self._simple_call("list_subdatasets", {"path": str(path)}, timeout=timeout)

    def probe_subdataset(self, uri: str, timeout: float = 15.0) -> dict[str, Any]:
        """探测单个子数据集（P3-02）。

        ``uri`` 是 GDAL 返回的数据源引用（可能是 ``NETCDF:"file.nc":Band1``），
        原样透传，绝不做路径解析。
        """
        return self._simple_call("probe_subdataset", {"uri": str(uri)}, timeout=timeout)

    def transform_coordinates(
        self,
        path: Path | str,
        points: list[tuple[float, float]] | list[list[float]],
        from_space: str,
        to_space: str,
        src_crs_wkt: str | None = None,
        dst_crs_wkt: str | None = None,
        convention: str = "corner",
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """ROI 坐标空间换算（像元 vs 地图 CRS 双向转换）。"""
        return self._simple_call(
            "transform_coordinates",
            {
                "path": str(path),
                "points": [list(p) for p in points],
                "from_space": from_space,
                "to_space": to_space,
                "src_crs_wkt": src_crs_wkt,
                "dst_crs_wkt": dst_crs_wkt,
                "convention": convention,
            },
            timeout=timeout,
        )

    def read_view_window(
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
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """请求把数据集重投影到统一视图网格后读取（跨 CRS 叠加用，不在 GUI 端拼像素）。"""
        args: dict[str, Any] = {
            "path": str(path),
            "view_crs_wkt": view_crs_wkt,
            "x_min": float(x_min),
            "y_min": float(y_min),
            "x_max": float(x_max),
            "y_max": float(y_max),
            "out_w": int(out_w),
            "out_h": int(out_h),
            "resampling": resampling,
        }
        if bands is not None:
            args["bands"] = bands
        return self._simple_call("read_view_window", args, timeout=timeout)

    def _join_readers(self, timeout: float = 2.0) -> None:
        """等读者线程结束：子进程已退出时管道会 EOF，线程随即返回。

        不 join 就让守护线程活到解释器退出，Windows 上会在关闭阶段随机崩
        （STATUS_STACK_BUFFER_OVERRUN），表现为测试与关窗时的异常退出码。
        """
        if self._proc is not None:
            for stream in (self._proc.stdout, self._proc.stderr, self._proc.stdin):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t.is_alive():
                t.join(timeout=timeout)

    def __del__(self) -> None:
        """对象回收时兜底终止子进程。

        单个 GDAL worker 常驻内存约 66 MB（加载 QGIS 的 osgeo）。
        任何异常路径若漏掉 stop()/kill()，孤儿进程会持续占用内存，
        累积到一定程度后新 worker 的启动握手会因内存分配困难而超时
        （表现为 `WorkerTimeoutError: Worker 启动握手超时`）。
        这里做最后一道兜底：确保子进程不会随调用方一起被遗弃。

        解释器退出阶段调用时资源可能已不可用，因此全部操作都吞掉异常。
        """
        try:
            proc = getattr(self, "_proc", None)
            if proc is None:
                return
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)
        except Exception:
            pass

    def stop(self, timeout: float = 5.0) -> int:
        """向 Worker 发送 shutdown 命令并等待进程退出。"""
        if not self.is_alive or self._proc is None:
            # 进程已退出或未存活：仍需确保子进程被回收
            # （is_alive 判断与真实状态可能不一致，残留子进程会持续占内存）。
            self._stop_event.set()
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)
            except Exception:
                pass
            self._join_readers()
            return 0

        try:
            env = new_message(
                Kind.request,
                self.worker_id,
                {"action": "shutdown", "arguments": {}},
                generation=self.generation,
            )
            self.send_envelope(env)
            self._proc.wait(timeout=timeout)
        except Exception:
            self.kill()

        self._stop_event.set()
        self._join_readers()
        return self._proc.returncode if self._proc else 0

    def kill(self) -> None:
        """强制终止 Worker 进程。"""
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            except Exception:
                pass
        self._join_readers()
