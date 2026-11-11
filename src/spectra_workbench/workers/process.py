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
from enum import StrEnum
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


class WorkerState(StrEnum):
    """Worker 生命周期状态。"""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


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

    if provider_id in {"gdal", "plugin"}:
        gdal_info = resolve_gdal_runtime(root)
        if gdal_info.get("python"):
            py_path = Path(gdal_info["python"])
            base_env.update(gdal_info.get("env", {}))
            return py_path, base_env
        return Path(sys.executable), base_env

    if provider_id == "rasterio":
        candidates = [
            root / "runtimes" / "rasterio" / "python.exe",
            root / "runtimes" / "rasterio" / "Scripts" / "python.exe",
            root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe",
        ]
        py_path = next((p for p in candidates if p.exists()), Path(sys.executable))
        return py_path, base_env

    if provider_id == "science":
        candidates = [
            root / "runtimes" / "science" / "python.exe",
            root / "runtimes" / "science" / "Scripts" / "python.exe",
            root / ".venvs" / "sw-science" / "Scripts" / "python.exe",
        ]
        py_path = next((p for p in candidates if p.exists()), Path(sys.executable))
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

        # S2：显式生命周期状态机。并发 start 共享同一次启动结果。
        self._state = WorkerState.STOPPED
        self._state_lock = threading.RLock()
        self._state_cv = threading.Condition(self._state_lock)
        self._cleanup_lock = threading.Lock()
        self._start_result: Envelope | None = None
        self._start_error: BaseException | None = None
        self._start_owner: int | None = None
        self._start_cancel = threading.Event()
        self._startup_timings: dict[str, Any] = {}
        self._generation_started = False
        self._active_generation: int | None = None

        # 配置大控制消息落盘溢出目录
        try:
            spill_root = Path(tempfile.gettempdir()) / "spectra_workbench_spill"
            spill_root.mkdir(parents=True, exist_ok=True)
            self.spill_dir: Path | None = spill_root
        except Exception:
            self.spill_dir = None

        # 解析命令与环境：统一先按契约解析，再应用自定义覆盖
        self._python_exe, self._env = resolve_worker_command(provider_id, self.project_root)
        if custom_python:
            self._python_exe = Path(custom_python)
        if custom_env:
            self._env.update(custom_env)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state

    @property
    def startup_timings(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._startup_timings)

    def add_listener(self, listener: Callable[[Envelope], None]) -> None:
        """添加消息监听器（每收到一条有效协议消息均回调）。"""
        self._listeners.append(listener)

    def start(self, timeout: float = 15.0) -> Envelope:
        """启动 Worker 并共享同一次握手结果（STOPPED→STARTING→READY）。

        并发调用者不会各自拉起进程；第一个调用者成为 owner，其余在条件变量上
        等待同一个 ready/异常。每次真正启动绑定新的 generation、进程和管道。
        """
        with self._state_cv:
            if (
                self._state is WorkerState.READY
                and self.is_alive
                and self._ready_envelope is not None
            ):
                return self._ready_envelope

            if self._state is WorkerState.STARTING:
                deadline = time.monotonic() + timeout
                while self._state is WorkerState.STARTING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerTimeoutError(f"等待其他线程完成 Worker 启动超时 ({timeout}s)")
                    self._state_cv.wait(remaining)
                if self._state is WorkerState.READY and self._ready_envelope is not None:
                    return self._ready_envelope
                if self._start_error is not None:
                    raise self._start_error
                if self._state is WorkerState.STOPPED:
                    raise WorkerProcessError("Worker 在启动过程中被停止")
                raise WorkerProcessError(f"Worker 启动未完成，当前状态 {self._state.value}")

            if self._state is WorkerState.STOPPING:
                deadline = time.monotonic() + timeout
                while self._state is WorkerState.STOPPING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerTimeoutError("等待 Worker 停止超时")
                    self._state_cv.wait(remaining)

            if self._state is WorkerState.READY and not self.is_alive:
                self._state = WorkerState.STOPPED

            # 成为本次启动的 owner。
            self._state = WorkerState.STARTING
            self._start_result = None
            self._start_error = None
            self._start_owner = threading.get_ident()
            self._start_cancel = threading.Event()
            if self._generation_started:
                self.generation = self._guard.restart()
            else:
                self._generation_started = True
                self._guard.current_generation = self.generation
            generation = self.generation
            self._active_generation = generation
            self._hello_envelope = None
            self._ready_envelope = None
            self.received_envelopes.clear()
            self._startup_timings = {
                "generation": generation,
                "spawn_start": time.monotonic(),
            }
            deadline = time.monotonic() + timeout

        try:
            proc, stop_event = self._spawn_generation(generation, deadline)
            ready = self._wait_ready(proc, generation, stop_event, deadline, timeout)
        except BaseException as exc:
            self._fail_start(exc)
            raise

        with self._state_cv:
            self._startup_timings["ready"] = time.monotonic()
            self._state = WorkerState.READY
            self._start_result = ready
            self._start_error = None
            self._state_cv.notify_all()
        return ready

    def _spawn_generation(
        self, generation: int, deadline: float
    ) -> tuple[subprocess.Popen[bytes], threading.Event]:
        cmd = [
            str(self._python_exe),
            "-m",
            "spectra_workbench.workers.entrypoint",
            self.provider_id,
            "--worker-id",
            self.worker_id,
            "--generation",
            str(generation),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                bufsize=0,
            )
        except Exception as exc:
            raise WorkerProcessError(f"启动 Worker 进程失败: {exc} (cmd={cmd})") from exc

        stop_event = threading.Event()
        with self._state_cv:
            if self._state is not WorkerState.STARTING or self._start_cancel.is_set():
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=2.0)
                raise WorkerProcessError("Worker 在进程创建后、握手前被停止")
            self._proc = proc
            self._active_generation = generation
            self._stop_event = stop_event
            self._stdout_thread = threading.Thread(
                target=self._read_stdout_loop,
                args=(proc, stop_event, generation),
                name=f"worker-{self.worker_id}-g{generation}-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                args=(proc, stop_event),
                name=f"worker-{self.worker_id}-g{generation}-stderr",
                daemon=True,
            )
            self._startup_timings["spawn_done"] = time.monotonic()
            self._stdout_thread.start()
            self._stderr_thread.start()
        return proc, stop_event

    def _wait_ready(
        self,
        proc: subprocess.Popen[bytes],
        generation: int,
        stop_event: threading.Event,
        deadline: float,
        timeout: float,
    ) -> Envelope:
        while time.monotonic() < deadline:
            with self._state_cv:
                if (
                    self._state is not WorkerState.STARTING
                    or self._start_cancel.is_set()
                    or stop_event.is_set()
                ):
                    raise WorkerProcessError("Worker 在启动过程中被停止")
                if self._active_generation == generation and self._ready_envelope is not None:
                    return self._ready_envelope
            rc = proc.poll()
            if rc is not None:
                stderr_preview = "".join(self.stderr_lines[-20:])[-600:]
                raise WorkerProcessError(
                    f"Worker 进程在握手期间提前退出 (code={rc}):\n{stderr_preview}"
                )
            time.sleep(0.02)

        elapsed = time.monotonic() - float(self._startup_timings.get("spawn_start", 0))
        hello_seen = self._hello_envelope is not None
        proc_alive = self.is_alive
        stderr_preview = "".join(self.stderr_lines[-20:])[-600:]
        raise WorkerTimeoutError(
            f"Worker 启动握手超时 ({timeout}s，实测 {elapsed:.2f}s): "
            f"hello_received={hello_seen}, ready_received=False, "
            f"proc_alive_at_timeout={proc_alive}, "
            f"timings={self.startup_timings}, stderr_tail={stderr_preview!r}"
        )

    def _fail_start(self, exc: BaseException) -> None:
        with self._cleanup_lock:
            stop_event = self._stop_event
            stop_event.set()
            proc = self._proc
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2.0)
            self._join_readers(timeout=2.0)
        with self._state_cv:
            if self._state is WorkerState.STOPPING or self._start_cancel.is_set():
                self._state = WorkerState.STOPPED
            else:
                self._state = WorkerState.FAILED
            self._start_error = exc
            self._startup_timings["failed"] = time.monotonic()
            self._state_cv.notify_all()

    def _read_stdout_loop(
        self,
        proc: subprocess.Popen[bytes] | None = None,
        stop_event: threading.Event | None = None,
        generation: int | None = None,
    ) -> None:
        """后台分帧读取 stdout；绑定不可变的本次进程/管道/代际。"""
        parser = JsonlStreamParser()
        proc = proc or self._proc
        stop_event = stop_event or self._stop_event
        generation = self._active_generation if generation is None else generation
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout

        while not stop_event.is_set():
            try:
                line = stdout.readline()
                if not line:
                    break
                envelopes = parser.feed(line)
                for env in envelopes:
                    if generation is not None and env.generation != generation:
                        self._guard.rejected_stale += 1
                        continue
                    if not self._guard.accept(env):
                        continue
                    self._on_envelope_received(env)
            except Exception as exc:
                self.stderr_lines.append(f"[Client Protocol Error] {exc}\n")

        # 刷新最后遗留
        try:
            for env in parser.close():
                if generation is not None and env.generation != generation:
                    self._guard.rejected_stale += 1
                    continue
                if self._guard.accept(env):
                    self._on_envelope_received(env)
        except Exception:
            pass

    def _read_stderr_loop(
        self,
        proc: subprocess.Popen[bytes] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """后台读取 stderr 并记录，防止管道填满造成子进程死锁。"""
        proc = proc or self._proc
        stop_event = stop_event or self._stop_event
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        while not stop_event.is_set():
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
        if env.generation != self._guard.current_generation:
            self._guard.rejected_stale += 1
            return
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
            with self._state_lock:
                self._startup_timings.setdefault("hello", time.monotonic())
        elif env.kind is Kind.ready and self._ready_envelope is None:
            self._ready_envelope = env
            with self._state_lock:
                self._startup_timings.setdefault("ready", time.monotonic())

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
                        outputs=[
                            {"uri": art, "data_type": "artifact"}
                            for art in env.payload.get("artifact_ids", [])
                        ],
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

            stderr_tail = "".join(self.stderr_lines[-60:])[-4000:]
            raise WorkerTimeoutError(
                f"任务 {spec.task_id} 执行超时 ({timeout}s)；worker stderr 尾部:\n{stderr_tail}"
            )
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

    def apply_credential_profile(self, profile: Any, timeout: float = 5.0) -> dict[str, Any]:
        """将凭据 Profile 配置发送给 Worker 进程，配置环境变量与 GDAL VSI 选项（P8-03）。"""
        from spectra_workbench.domain.credentials import CredentialProfile, CredentialStore

        if isinstance(profile, str):
            store = CredentialStore()
            prof = store.get_profile(profile)
            if prof is None:
                raise WorkerProcessError(f"Credential profile not found: {profile}")
        elif isinstance(profile, CredentialProfile):
            prof = profile
        else:
            raise TypeError(f"Expected CredentialProfile or str, got {type(profile)}")

        env_vars = prof.to_env_vars()
        self._env.update(env_vars)
        if not self.is_alive:
            return {"status": "ok", "deferred": True, "applied_keys": list(env_vars.keys())}

        env = self.send_request(
            "configure_credentials",
            {"profile_id": prof.profile_id, "env_vars": env_vars},
            timeout=timeout,
        )
        if env.kind is Kind.error:
            raise RuntimeError(f"配置凭据失败: {env.payload.get('message')}")
        res = env.payload.get("data") if "data" in env.payload else env.payload
        return res if isinstance(res, dict) else {"status": "ok"}

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
        args: dict[str, Any] = {
            "path": str(path),
            "x": float(x),
            "y": float(y),
            "convention": convention,
        }
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

    def describe_driver(self, format_name: str, timeout: float = 10.0) -> dict[str, Any]:
        """返回驱动的读写能力声明与高级创建选项（P5-05）。"""
        return self._simple_call(
            "describe_driver", {"format_name": str(format_name)}, timeout=timeout
        )

    def transform_point(
        self, x: float, y: float, src_wkt: str, dst_wkt: str, timeout: float = 10.0
    ) -> dict[str, Any]:
        return self._simple_call(
            "transform_point",
            {"x": float(x), "y": float(y), "src_wkt": src_wkt, "dst_wkt": dst_wkt},
            timeout=timeout,
        )

    def transform_bounds(
        self, path: Path | str, view_crs_wkt: str, timeout: float = 15.0
    ) -> dict[str, Any]:
        """数据集范围换算到视图 CRS。"""
        return self._simple_call(
            "transform_bounds", {"path": str(path), "view_crs_wkt": view_crs_wkt}, timeout=timeout
        )

    def probe_vector(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        """探测矢量数据集的基础元数据（P3-02）。"""
        return self._simple_call("probe_vector", {"path": str(path)}, timeout=timeout)

    def read_vector_page(
        self,
        path: Path | str,
        layer_index: int = 0,
        start: int = 0,
        count: int = 100,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """按范围读取矢量图层的一页要素（P5-03 分页属性表）。"""
        return self._simple_call(
            "read_vector_page",
            {
                "path": str(path),
                "layer_index": int(layer_index),
                "start": int(start),
                "count": int(count),
            },
            timeout=timeout,
        )

    def list_subdatasets(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        """列出容器数据集的子数据集（P3-02）。"""
        return self._simple_call("list_subdatasets", {"path": str(path)}, timeout=timeout)

    def probe_multidim(self, path: Path | str, timeout: float = 15.0) -> dict[str, Any]:
        """多维数据树（P8-01）：group/attributes/dimensions/arrays。

        只读结构信息，不触发任何全量数组读取；HDF5 依赖引擎协议读写同一模型。
        """
        return self._simple_call("probe_multidim", {"path": str(path)}, timeout=timeout)

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
        """等读者线程结束，再关闭管道。

        不 join 就让守护线程活到解释器退出，Windows 上会在关闭阶段随机崩
        （STATUS_STACK_BUFFER_OVERRUN），表现为测试与关窗时的异常退出码。

        **顺序修正（第 37 轮）**：旧实现先 ``stream.close()`` 再 ``join()``。
        在 Windows 上关闭仍被 ``readline()`` 阻塞的管道会让读者线程行为
        不确定，可能 join 超时放弃而残留守护线程。正常路径下 stop()/kill()
        已让子进程退出、管道 EOF，读者线程会自行返回，因此改为
        **先 join、后 close**；只有 join 超时的异常路径才强制关管道并二次 join。
        """
        readers = [t for t in (self._stdout_thread, self._stderr_thread) if t is not None]
        for t in readers:
            if t.is_alive():
                t.join(timeout=timeout)

        streams: tuple[Any, ...] = ()
        if self._proc is not None:
            streams = (self._proc.stdout, self._proc.stderr, self._proc.stdin)

        still_alive = [t for t in readers if t.is_alive()]
        if still_alive:
            for stream in streams:
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
            for t in still_alive:
                t.join(timeout=1.0)

        for stream in streams:
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

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
        """停止 Worker：READY 走 shutdown，STARTING 直接终止本次挂起/启动进程。"""
        with self._state_cv:
            if self._state is WorkerState.STOPPED and (
                self._proc is None or self._proc.poll() is not None
            ):
                return 0
            previous = self._state
            self._state = WorkerState.STOPPING
            self._start_cancel.set()
            self._stop_event.set()
            proc = self._proc
            self._state_cv.notify_all()

        rc = 0
        if proc is not None:
            if proc.poll() is None:
                if previous is WorkerState.READY:
                    with contextlib.suppress(Exception):
                        env = new_message(
                            Kind.request,
                            self.worker_id,
                            {"action": "shutdown", "arguments": {}},
                            generation=self.generation,
                        )
                        self.send_envelope(env)
                    try:
                        proc.wait(timeout=timeout)
                    except Exception:
                        with contextlib.suppress(Exception):
                            proc.kill()
                            proc.wait(timeout=2.0)
                else:
                    with contextlib.suppress(Exception):
                        proc.kill()
                        proc.wait(timeout=2.0)
            rc = int(proc.returncode or 0)

        with self._cleanup_lock:
            self._stop_event.set()
            self._join_readers(timeout=timeout)

        with self._state_cv:
            self._state = WorkerState.STOPPED
            self._state_cv.notify_all()
        return rc

    def kill(self) -> None:
        """强制终止 Worker 进程并收敛到 STOPPED。"""
        with self._state_cv:
            self._start_cancel.set()
            self._state = WorkerState.STOPPING
            self._stop_event.set()
            proc = self._proc
            self._state_cv.notify_all()
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)
        with self._cleanup_lock:
            self._stop_event.set()
            self._join_readers(timeout=2.0)
        with self._state_cv:
            self._state = WorkerState.STOPPED
            self._state_cv.notify_all()
