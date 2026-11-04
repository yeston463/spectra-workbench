"""进程级硬超时执行器：把一条命令放进被监控的子进程里跑，超时就杀掉整棵进程树。

用途：GUI/引擎测试可能因模态对话框、子进程握手或线程挂起而永久阻塞。
pytest 自带的 faulthandler_timeout 只打印堆栈、不会终止用例，因此需要外层硬超时。

    python scripts/run_with_timeout.py --timeout 300 --log work/gui_tests.log ^
        -- .venv/Scripts/python.exe -m pytest -m gui -o faulthandler_timeout=60

行为约定：
- 子进程输出实时写入 --log（未过滤），便于事后定位；
- 超时后用 taskkill /T /F 结束子进程树（含它派生的 worker），并写 <log>.timeout.json；
- 超时返回退出码 124；子进程自身非零则透传其退出码。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


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


def run(cmd: list[str], timeout: float, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    with log_path.open("ab") as log:
        log.write(f"\n=== run_with_timeout start {datetime.now().isoformat()} "
                  f"timeout={timeout}s cmd={' '.join(cmd)} ===\n".encode())
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
            return code if code is not None else 1
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
            log_path.with_suffix(log_path.suffix + ".timeout.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f">>> 硬超时 {timeout}s：已清理 {len(killed)} 个进程，证据见 "
                  f"{log_path} 与 {log_path}.timeout.json", file=sys.stderr, flush=True)
            return 124


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=float, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("缺少被执行的命令", file=sys.stderr)
        return 2
    return run(cmd, args.timeout, Path(args.log).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
