"""进程级硬超时执行器：把一条命令放进被监控的子进程里跑，超时就杀掉整棵进程树。

用途：GUI/引擎测试可能因模态对话框、子进程握手或线程挂起而永久阻塞。
pytest 自带的 faulthandler_timeout 只打印堆栈、不会终止用例，因此需要外层硬超时。

S1 退出验收：本脚本同时是外层监督器。传入 ``--result-json`` 时，它会在子进程
真实退出后写统一结果，并用 ``exit_mode``、``pytest_exit_code``、``child_exit_code``
和残留进程决定 ``clean_exit_verified``。Bypass 模式永远不能得到 true。

行为约定：
- 子进程输出实时写入 --log（未过滤），便于事后定位。
- 超时后用 taskkill /T /F 结束子进程树（含它派生的 worker），返回 124。
- 子进程自身非零则透传其退出码。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# 直接执行时脚本目录在 sys.path[0]；测试导入时补一次。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from source_snapshot import compute_source_snapshot  # noqa: E402

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _descendant_tree(root_pid: int) -> list[int]:
    """root_pid 及其全部后代（用 PowerShell CIM 查询，不依赖第三方库）。"""
    pids, frontier = [], [root_pid]
    while frontier:
        pid = frontier.pop()
        pids.append(pid)
        probe = f'(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid}").ProcessId'
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", probe],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            continue
        for line in out.stdout.splitlines():
            if line.strip().isdigit():
                frontier.append(int(line.strip()))
    return pids


def _pid_alive(pid: int) -> bool:
    """OpenProcess 查询 PID 是否仍存在；仅用于退出后的残留证据。"""
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # type: ignore[attr-defined]
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return True


def kill_tree(root_pid: int) -> list[int]:
    """结束 root_pid 的整棵进程树；返回实际尝试结束的 PID。"""
    killed: list[int] = []
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(root_pid)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        killed.append(root_pid)
        print(
            f">>> 已结束进程树 root={root_pid} (taskkill rc={proc.returncode})",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # taskkill 不可用时退回逐个 CIM 查询
        print(f">>> taskkill 失败，改用 CIM 枚举：{exc}", file=sys.stderr, flush=True)
    for pid in _descendant_tree(root_pid):
        if pid == root_pid:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20)
            killed.append(pid)
        except Exception:
            pass
    return killed


def _read_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp_{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_result(
    result_json: Path | None,
    *,
    run_id: str,
    snapshot: str,
    exit_mode: str,
    pytest_exit_code: int | None,
    child_exit_code: int,
    timed_out: bool,
    residual_processes: list[int],
    cleanup_errors: list[str],
    log_path: Path,
    cmd: list[str],
    started_at: str,
) -> bool:
    preliminary = _read_json(result_json)
    if preliminary.get("run_id"):
        run_id = str(preliminary["run_id"])
    if preliminary.get("source_snapshot"):
        snapshot = str(preliminary["source_snapshot"])
    if pytest_exit_code is None and preliminary.get("pytest_exit_code") is not None:
        pytest_exit_code = int(preliminary["pytest_exit_code"])
    clean = (
        exit_mode == "clean"
        and not timed_out
        and child_exit_code == 0
        and pytest_exit_code == 0
        and not residual_processes
        and not cleanup_errors
    )
    result = {
        "schema_version": "1.0",
        "run_id": run_id,
        "source_snapshot": snapshot,
        "exit_mode": exit_mode,
        "bypass_finalization": exit_mode == "bypass",
        "pytest_exit_code": pytest_exit_code,
        "child_exit_code": child_exit_code,
        "clean_exit_verified": bool(clean),
        "timed_out": timed_out,
        "cleanup_errors": cleanup_errors,
        "residual_processes": residual_processes,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "log": str(log_path),
        "command": cmd,
        "pytest_result": preliminary or None,
    }
    if result_json is None:
        return clean
    try:
        _atomic_write_json(result_json, result)
    except Exception as exc:
        cleanup_errors.append(f"write_result_failed: {type(exc).__name__}: {exc}")
        print(f">>> 无法写入统一结果 {result_json}: {exc}", file=sys.stderr, flush=True)
        return False
    return clean


def run(
    cmd: list[str],
    timeout: float,
    log_path: Path,
    *,
    result_json: Path | None = None,
    exit_mode: str = "clean",
    run_id: str | None = None,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    started_at = _utc_now()
    preliminary = _read_json(result_json)
    run_id = run_id or preliminary.get("run_id") or uuid.uuid4().hex
    snapshot = preliminary.get("source_snapshot") or compute_source_snapshot(Path.cwd())
    cleanup_errors: list[str] = []

    with log_path.open("ab") as log:
        log.write(
            f"\n=== run_with_timeout start {datetime.now().isoformat()} "
            f"timeout={timeout}s exit_mode={exit_mode} cmd={' '.join(cmd)} ===\n".encode()
        )
        log.flush()
        child = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
        )
        try:
            code = child.wait(timeout=timeout)
            elapsed = time.monotonic() - started
            log.write(f"\n=== child exited rc={code} elapsed={elapsed:.1f}s ===\n".encode())
            code = code if code is not None else 1
            residual = [
                pid
                for pid in _descendant_tree(child.pid)
                if pid != child.pid and _pid_alive(pid)
            ]
            _write_result(
                result_json,
                run_id=run_id,
                snapshot=snapshot,
                exit_mode=exit_mode,
                pytest_exit_code=None,
                child_exit_code=code,
                timed_out=False,
                residual_processes=residual,
                cleanup_errors=cleanup_errors,
                log_path=log_path,
                cmd=cmd,
                started_at=started_at,
            )
            return code
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            killed = kill_tree(child.pid)
            log.write(
                f"\n=== HARD TIMEOUT after {elapsed:.1f}s (limit {timeout}s); "
                f"killed pids={killed} ===\n".encode()
            )
            evidence = {
                "timeout_seconds": timeout,
                "elapsed_seconds": round(elapsed, 1),
                "root_pid": child.pid,
                "killed_pids": killed,
                "log": str(log_path),
                "command": cmd,
                "at": datetime.now().isoformat(),
            }
            with contextlib.suppress(Exception):
                log_path.with_suffix(log_path.suffix + ".timeout.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            _write_result(
                result_json,
                run_id=run_id,
                snapshot=snapshot,
                exit_mode=exit_mode,
                pytest_exit_code=None,
                child_exit_code=124,
                timed_out=True,
                residual_processes=killed,
                cleanup_errors=cleanup_errors,
                log_path=log_path,
                cmd=cmd,
                started_at=started_at,
            )
            print(
                f">>> 硬超时 {timeout}s：已清理 {len(killed)} 个进程，证据："
                f"{log_path} / {log_path}.timeout.json",
                file=sys.stderr,
                flush=True,
            )
            return 124


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=float, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--result-json", default=None)
    ap.add_argument("--exit-mode", choices=("clean", "bypass"), default="clean")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("缺少被执行的命令", file=sys.stderr)
        return 2
    return run(
        cmd,
        args.timeout,
        Path(args.log).resolve(),
        result_json=Path(args.result_json).resolve() if args.result_json else None,
        exit_mode=args.exit_mode,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
