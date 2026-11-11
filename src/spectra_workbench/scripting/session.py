"""P7-02 隔离脚本会话：GDAL / Rasterio 会话分别导入原生 API。

设计（符合 docs/plan §3.1「环境隔离原则」）：
- **引擎各用各自的真实解释器**：GDAL 用 doctor 解析的 QGIS/conda 运行时，
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进candidates = [
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进    root / "runtimes" / "rasterio" / "python.exe",
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进    root / "runtimes" / "rasterio" / "Scripts" / "python.exe",
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进    root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe",
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进]
  Rasterio 用项目里的 ``.venvs\\sw-rasterio\\Scripts\\python.exe``。GUI 进py = next((p for p in candidates if p.exists()), candidates[-1])
  永远不 ``import osgeo`` / ``rasterio``——本模块只启动子进程，不传递原生句柄。
- **用户脚本在 engine 解释器里 ``exec``**：``from osgeo import gdal`` /
  ``import rasterio`` 都在引擎环境内真实执行，受 engine 环境变量影响。
- **停止 / 日志 / 成果登记 / 会话重启**：
  - ``stop()`` 终止子进程；后续 ``run()`` 自动重开新会话；
  - ``logs/stdout.log`` 与 ``logs/stderr.log`` 写到会话目录；
  - 脚本通过 ``register_artifact(path)`` 显式登记成果，``artifacts.json``
  记录路径 + SHA-256 + 大小 + mtime，作为独立可审计的成果清单；
  - 会话元数据 ``session.json`` 记录 engine、python 路径、启动时间与
  会话 ID，支持重启后的可追溯性。
- 会话**非线程安全**：调用方自行保证串行；``ScriptSession.is_alive``
  返回二进制状态，不冒充伪状态。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "SessionError",
    "SESSION_ENGINES",
    "ScriptSession",
]

SESSION_ENGINES: tuple[str, ...] = ("gdal", "rasterio")

_DOC_DEFAULT = """
"""
_ASSERT_DEFAULT = """
"""


class SessionError(RuntimeError):
    """脚本会话参数/环境/状态错误。"""


def _project_root() -> Path:
    from spectra_workbench.app.doctor import find_project_root

    return Path(find_project_root())


def _resolve_engine_python(engine: str, root: Path) -> Path:
    """按 engine 返回真实解释器路径；GUI 进程不导入任何原生地理库。"""
    if engine == "gdal":
        from spectra_workbench.app.doctor import resolve_gdal_runtime

        info = resolve_gdal_runtime(root)
        py = Path(info["python"])
    elif engine == "rasterio":
        py = root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe"
    else:
        raise SessionError(f"未知脚本会话引擎 {engine!r}；可用：{', '.join(SESSION_ENGINES)}")
    if not py.exists():
        raise SessionError(f"未能找到 {engine} 解释器：{py}")
    return py


#: 引擎侧 bootstrap：stdlib only（不 import native）；逐行写入 stdout/stderr
#: 接收三个参数：session_dir、engine、script_path。
_BOOTSTRAP_TEMPLATE = r"""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

session_dir = Path(sys.argv[1])
engine = sys.argv[2]
script_path = Path(sys.argv[3])
logs_dir = session_dir / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)
stdout_path = logs_dir / "stdout.log"
stderr_path = logs_dir / "stderr.log"

ns: dict[str, object] = {
    "__name__": "__spectra_script__",
    "__file__": str(script_path),
    "engine": engine,
    "workspace": str(session_dir),
    "artifacts": [],
}


def register_artifact(path: object) -> None:
    from hashlib import sha256
    from datetime import datetime

    p = Path(str(path)).resolve()
    if not p.exists():
        raise FileNotFoundError(f"成果不存在：{p}")
    stat = p.stat()
    ns["artifacts"].append({  # type: ignore[index,has-type]
        "path": str(p),
        "sha256": sha256(p.read_bytes()).hexdigest(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "registered_at": datetime.now().isoformat(),
    })


ns["register_artifact"] = register_artifact
code = script_path.read_text(encoding="utf-8-sig")
exit_code = 0
error_text = ""
try:
    exec(compile(code, str(script_path), "exec"), ns, ns)
except BaseException:
    traceback.print_exc()
    exit_code = 1

with open(stdout_path, "ab") as fh:
    fh.flush()
try:
    # 成果清单：需要显式 register_artifact；不靠 Glob 自动捡
    (session_dir / "artifacts.json").write_text(
        json.dumps(ns["artifacts"] if isinstance(ns.get("artifacts"), list) else [],
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
except Exception:
    traceback.print_exc()
    exit_code = 2
session_result = {"exit_code": exit_code}
(session_dir / "session_result.json").write_text(
    json.dumps(session_result, ensure_ascii=False), encoding="utf-8"
)
"""


class ScriptSession:
    """单个引擎的一次脚本会话（真实引擎解释器子进程）。"""

    def __init__(
        self,
        engine: str,
        *,
        session_id: str | None = None,
        session_dir: Path | str | None = None,
    ) -> None:
        engine = str(engine or "").strip().lower()
        if engine not in SESSION_ENGINES:
            raise SessionError(f"未知脚本会话引擎 {engine!r}；可用：{', '.join(SESSION_ENGINES)}")
        self.engine = engine
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._root = _project_root()
        self._python = _resolve_engine_python(engine, self._root)
        self.session_dir = (
            Path(session_dir).resolve()
            if session_dir is not None
            else self._root / "work" / "scripting" / f"{engine}-{self.session_id}"
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "logs").mkdir(parents=True, exist_ok=True)
        self._proc: subprocess.Popen[bytes] | None = None
        self._bootstrapped = False
        self._bootstrap_path = self.session_dir / "_bootstrap.py"

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def python_path(self) -> Path:
        """该会话执行的**真实**引擎解释器路径。"""
        return self._python

    @property
    def is_alive(self) -> bool:
        """当前会话子进程是否仍然存活。"""
        return self._proc is not None and self._proc.poll() is None

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "engine": self.engine,
            "python_path": str(self.python_path),
            "session_dir": str(self.session_dir),
            "alive": self.is_alive,
        }

    # ------------------------------------------------------------------
    # 停止 / 启动
    # ------------------------------------------------------------------
    def stop(self, timeout: float = 5.0) -> int:
        """停止会话子进程；返回退出码，进程已死返回 0。"""
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
            return 0
        deadline = time.monotonic() + timeout
        try:
            while self._proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            if self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait(timeout=3)
        code = self._proc.returncode or 0
        self._proc = None
        return code

    def _write_session_json(self) -> None:
        doc = {
            "session_id": self.session_id,
            "engine": self.engine,
            "python_path": str(self.python_path),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # 运行脚本
    # ------------------------------------------------------------------
    def run(self, script: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """在引擎解释器里执行脚本；返回 stdout/stderr 尾部与成果清单。"""
        if not isinstance(script, str) or not script.strip():
            raise SessionError("script 必须是非空 Python 源码")
        if self.is_alive:
            self.stop(timeout=3.0)

        self._write_session_json()
        self._write_bootstrap()
        script_path = self.session_dir / "script.py"
        script_path.write_text(script, encoding="utf-8")

        # 清理上一次会话残留（保证每次 run 都是新的"成果登记"）
        for junk in ("artifacts.json", "session_result.json"):
            (self.session_dir / junk).unlink(missing_ok=True)

        stdout_path = self.session_dir / "logs" / "stdout.log"
        stderr_path = self.session_dir / "logs" / "stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        child_env = dict(os.environ)
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        try:
            with stdout_path.open("ab") as out_fh, stderr_path.open("ab") as err_fh:
                self._proc = subprocess.Popen(
                    [
                        str(self.python_path),
                        str(self._bootstrap_path),
                        str(self.session_dir),
                        self.engine,
                        str(script_path),
                    ],
                    stdout=out_fh,
                    stderr=err_fh,
                    cwd=str(self.session_dir),
                    stdin=subprocess.DEVNULL,
                    env=child_env,
                )
                try:
                    exit_code = self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                    raise SessionError(f"脚本会话执行超时 {timeout}s：{exc}") from exc
        finally:
            # run() 结束即会话完整；下次 run 将以全新进程重启
            self._proc = None

        artifacts = self._read_artifacts()
        return {
            "session_id": self.session_id,
            "engine": self.engine,
            "exit_code": exit_code,
            "stdout_tail": self._tail(stdout_path),
            "stderr_tail": self._tail(stderr_path),
            "artifacts": artifacts,
            "logs_dir": str(self.session_dir / "logs"),
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _write_bootstrap(self) -> None:
        if self._bootstrapped and self._bootstrap_path.exists():
            self._bootstrapped = True
            return
        self._bootstrap_path.write_text(_BOOTSTRAP_TEMPLATE, encoding="utf-8")
        self._bootstrapped = True

    def _read_artifacts(self) -> list[dict[str, Any]]:
        path = self.session_dir / "artifacts.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _tail(self, path: Path, max_chars: int = 4000) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]
