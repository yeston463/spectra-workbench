"""Process-level hard timeout and the S1 exit supervisor.

The supervisor:
- owns the run identity (run_id, source snapshot) and injects it into the child;
- keeps the final result and the child preliminary result in different files;
- validates the child result's schema, identity, mode, completion and cleanup;
- uses a Windows Job Object to track the child and all descendants while they
  are still alive, so a child that exits after spawning a sleeping descendant
  cannot be recorded as clean;
- writes a unified final result whose ``clean_exit_verified`` is true only for
  a clean run with matching identity/mode, zero pytest and OS exit codes,
  complete cleanup observation and no residual processes.

Timeouts return 124. Invalid child evidence returns 92. A clean-mode run that
is not verified returns 90. Bypass functional success returns 0 here; test.ps1
then turns it into 91 so it cannot be mistaken for a release pass.
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
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from source_snapshot import compute_source_snapshot_details  # noqa: E402

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004

EXIT_RESULT_INVALID = 92
EXIT_CLEAN_NOT_VERIFIED = 90

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3

_JobObjectBasicLimitInformation = type(
    "JOBOBJECT_BASIC_LIMIT_INFORMATION",
    (ctypes.Structure,),
    {
        "_fields_": [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]
    },
)

_IO_COUNTERS = type(
    "IO_COUNTERS",
    (ctypes.Structure,),
    {
        "_fields_": [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]
    },
)

_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = type(
    "JOBOBJECT_EXTENDED_LIMIT_INFORMATION",
    (ctypes.Structure,),
    {
        "_fields_": [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]
    },
)

_JOBOBJECT_BASIC_PROCESS_ID_LIST = type(
    "JOBOBJECT_BASIC_PROCESS_ID_LIST",
    (ctypes.Structure,),
    {
        "_fields_": [
            ("NumberOfAssignedProcesses", ctypes.c_uint32),
            ("NumberOfProcessIdsInList", ctypes.c_uint32),
            ("ProcessIdList", ctypes.c_size_t * 1),
        ]
    },
)

_THREADENTRY32 = type(
    "THREADENTRY32",
    (ctypes.Structure,),
    {
        "_fields_": [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ThreadID", ctypes.c_uint32),
            ("th32OwnerProcessID", ctypes.c_uint32),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
        ]
    },
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "path_missing"
    if not path.exists():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid_json:{type(exc).__name__}:{exc}"
    if not isinstance(data, dict):
        return None, "not_an_object"
    return data, None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp_{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _descendant_tree(root_pid: int) -> list[int]:
    """Fallback only; post-exit recursion can miss already-exited middle layers."""
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
    except Exception as exc:
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


class ProcessJob:
    """Track the direct child and every descendant in a kill-on-close Job Object."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._handle: int | None = None
        self.error: str | None = None
        self.observation = "unknown"
        self._configure_win32()

    def _configure_win32(self) -> None:
        k = self._kernel32
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k.SetInformationJobObject.restype = ctypes.c_int
        k.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        k.AssignProcessToJobObject.restype = ctypes.c_int
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = ctypes.c_int
        k.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        k.TerminateJobObject.restype = ctypes.c_int
        k.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.CloseHandle.restype = ctypes.c_int
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k.Thread32First.restype = ctypes.c_int
        k.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
        k.Thread32Next.restype = ctypes.c_int
        k.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
        k.OpenThread.restype = ctypes.c_void_p
        k.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k.ResumeThread.restype = ctypes.c_uint32
        k.ResumeThread.argtypes = [ctypes.c_void_p]
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self._ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]

    def assign(self, child_pid: int, child_handle: int) -> bool:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.error = "CreateJobObject failed"
            return False
        self._handle = int(handle)
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.error = "SetInformationJobObject failed"
            return False
        if not self._kernel32.AssignProcessToJobObject(self._handle, child_handle):
            self.error = f"AssignProcessToJobObject failed (pid={child_pid})"
            return False
        self.observation = "job"
        return True

    def _resume_threads(self, pid: int) -> bool:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid:
            return False
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        resumed = 0
        try:
            if not self._kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                return False
            while True:
                if entry.th32OwnerProcessID == pid:
                    handle = self._kernel32.OpenThread(
                        THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if handle:
                        try:
                            if self._kernel32.ResumeThread(handle) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            self._kernel32.CloseHandle(handle)
                if not self._kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            self._kernel32.CloseHandle(snapshot)
        return resumed > 0

    def resume(self, pid: int, child_handle: int) -> bool:
        """Resume a CREATE_SUSPENDED child only after it is in the Job Object."""
        status = int(self._ntdll.NtResumeProcess(child_handle))
        if status == 0:
            return True
        # Fallback for hosts where NtResumeProcess is unavailable/blocked.
        return self._resume_threads(pid)

    def pids(self) -> list[int]:
        if self._handle is None:
            raise RuntimeError("job handle unavailable")
        capacity = 256
        for _ in range(4):
            size = 8 + capacity * ctypes.sizeof(ctypes.c_size_t)
            buf = (ctypes.c_char * size)()
            ret = ctypes.c_uint32(0)
            ok = self._kernel32.QueryInformationJobObject(
                self._handle,
                JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buf,
                size,
                ctypes.byref(ret),
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
            header = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))
            assigned = int(header[0])
            count = int(header[1])
            ptr = ctypes.cast(
                ctypes.byref(buf, 8), ctypes.POINTER(ctypes.c_size_t)
            )
            pids = [int(ptr[i]) for i in range(count)]
            if assigned <= capacity:
                return pids
            capacity = max(assigned + 32, capacity * 2)
        return pids

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _cleanup_report_state(path: Path | None) -> tuple[bool, list[str], dict[str, Any] | None, str | None]:
    data, error = _read_json(path)
    if error is not None or data is None:
        return False, [f"cleanup_report_{error}"], None, error
    if data.get("schema_version") != "1.0":
        return False, ["cleanup_report_invalid_schema"], data, "schema"
    errors = data.get("errors")
    if not isinstance(errors, list):
        return False, ["cleanup_report_errors_not_list"], data, "errors"
    complete = data.get("complete") is True
    return complete, [str(e) for e in errors], data, None


def _validate_child(
    child: dict[str, Any] | None,
    error: str | None,
    *,
    run_id: str,
    snapshot: str,
    expected_mode: str,
    child_exit_code: int,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if error is not None or child is None:
        return False, [f"child_result_{error or 'missing'}"]
    if child.get("schema_version") != "1.0":
        reasons.append("child_schema_mismatch")
    if child.get("run_id") != run_id:
        reasons.append("child_run_id_mismatch")
    if child.get("source_snapshot") != snapshot:
        reasons.append("child_source_snapshot_mismatch")
    if child.get("source_snapshot_verified") is not True:
        reasons.append("child_source_snapshot_unverified")
    actual_mode = child.get("exit_mode")
    if actual_mode != expected_mode:
        reasons.append(f"mode_mismatch:expected={expected_mode!r},actual={actual_mode!r}")
    if child.get("expected_exit_mode") != expected_mode:
        reasons.append("child_expected_mode_mismatch")
    if bool(child.get("bypass_finalization")) != (actual_mode == "bypass"):
        reasons.append("child_bypass_flag_mismatch")
    if child.get("completed") is not True:
        reasons.append("child_not_completed")
    if child.get("pytest_was_run") is not True:
        reasons.append("pytest_not_run")
    if child.get("finished_at") in (None, ""):
        reasons.append("child_finished_at_missing")
    if child.get("runner_error") not in (None, ""):
        reasons.append(f"child_runner_error:{child.get('runner_error')}")
    pytest_rc = child.get("pytest_exit_code")
    if not isinstance(pytest_rc, int):
        reasons.append("child_pytest_exit_code_not_int")
    elif pytest_rc != child_exit_code:
        reasons.append(f"child_pytest_exit_code_mismatch:{pytest_rc}!={child_exit_code}")
    if child.get("cleanup_check_complete") is not True:
        reasons.append("child_cleanup_check_incomplete")
    child_cleanup = child.get("cleanup_errors")
    if not isinstance(child_cleanup, list):
        reasons.append("child_cleanup_errors_not_list")
    elif child_cleanup:
        reasons.append("child_cleanup_errors_present")
    return not reasons, reasons


def run(
    cmd: list[str],
    timeout: float,
    log_path: Path,
    *,
    result_json: Path | None = None,
    child_result_json: Path | None = None,
    cleanup_report_json: Path | None = None,
    exit_mode: str = "clean",
    run_id: str | None = None,
) -> int:
    if (
        result_json is not None
        and child_result_json is not None
        and result_json.resolve() == child_result_json.resolve()
    ):
        print(">>> final result 与 child result 必须是不同文件", file=sys.stderr)
        return EXIT_RESULT_INVALID

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if result_json is not None:
        with contextlib.suppress(OSError):
            result_json.unlink()
    if child_result_json is not None:
        with contextlib.suppress(OSError):
            child_result_json.unlink()
    if cleanup_report_json is not None:
        with contextlib.suppress(OSError):
            cleanup_report_json.unlink()

    run_id = run_id or uuid.uuid4().hex
    snapshot_detail = compute_source_snapshot_details(Path.cwd())
    snapshot = snapshot_detail.value
    started_at = _utc_now()
    started = time.monotonic()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["SW_RUN_ID"] = run_id
    env["SW_SOURCE_SNAPSHOT"] = snapshot
    env["SW_SOURCE_SNAPSHOT_VERIFIED"] = "1" if snapshot_detail.verified else "0"
    env["SW_EXPECTED_EXIT_MODE"] = exit_mode
    if cleanup_report_json is not None:
        env["SW_CLEANUP_REPORT"] = str(cleanup_report_json)
    if child_result_json is not None:
        env["SW_CHILD_RESULT_JSON"] = str(child_result_json)

    cleanup_errors: list[str] = []
    process_observation = "unknown"
    residual_before: list[int] = []
    residual_after: list[int] = []
    child_exit_code: int | None = None
    timed_out = False
    child_result: dict[str, Any] | None = None
    child_error: str | None = None
    validation_ok = False
    validation_reasons: list[str] = []
    job = ProcessJob()
    timed_out = False
    startup_failure = False

    with log_path.open("ab") as log:
        log.write(
            f"\n=== run_with_timeout start {datetime.now().isoformat()} "
            f"timeout={timeout}s exit_mode={exit_mode} run_id={run_id} "
            f"cmd={' '.join(cmd)} ===\n".encode()
        )
        log.flush()
        child = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            # 先挂起创建，加入 Job 后再恢复；否则子进程可能在两步之间派生后代。
            creationflags=(
                CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB | CREATE_SUSPENDED
            ),
        )
        child_handle = int(getattr(child, "_handle", 0) or 0)
        assigned = job.assign(child.pid, child_handle)
        resumed = False
        if assigned:
            try:
                resumed = job.resume(child.pid, child_handle)
            except Exception as exc:
                cleanup_errors.append(f"job_resume: {type(exc).__name__}: {exc}")
        if not assigned or not resumed:
            startup_failure = True
            process_observation = "unknown"
            detail = "assign_failed" if not assigned else "resume_failed"
            cleanup_errors.append(f"job_start_failure: {detail}: {job.error or 'unknown'}")
            job.terminate()
            job.close()
            job.observation = "unknown"
            with contextlib.suppress(Exception):
                child.kill()
            try:
                child_exit_code = int(child.wait(timeout=5.0))
            except Exception:
                child_exit_code = 1
            log.write(
                f"\n=== startup failed before resume: {detail}; "
                f"killed suspended child pid={child.pid} rc={child_exit_code} ===\n".encode()
            )
        else:
            process_observation = "job"

        if not startup_failure:
            try:
                child_exit_code = child.wait(timeout=timeout)
                elapsed = time.monotonic() - started
                log.write(
                    f"\n=== child exited rc={child_exit_code} elapsed={elapsed:.1f}s ===\n".encode()
                )
                child_exit_code = int(child_exit_code if child_exit_code is not None else 1)
            except subprocess.TimeoutExpired:
                timed_out = True
                elapsed = time.monotonic() - started
                if job.observation == "job":
                    try:
                        residual_before = job.pids()
                    except Exception as exc:
                        cleanup_errors.append(
                            f"job_query_at_timeout: {type(exc).__name__}: {exc}"
                        )
                    job.terminate()
                killed = kill_tree(child.pid)
                residual_after = killed
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
                child_exit_code = 124

        # 父进程已退出，但 Job Object 仍在，可在中间层退出后继续看到存活后代。
        if job.observation == "job":
            try:
                residual_before = job.pids()
            except Exception as exc:
                cleanup_errors.append(f"job_query_after_exit: {type(exc).__name__}: {exc}")
            if residual_before:
                job.terminate()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        if not job.pids():
                            break
                    except Exception:
                        break
                    time.sleep(0.05)
            try:
                residual_after = job.pids()
            except Exception as exc:
                cleanup_errors.append(f"job_query_after_cleanup: {type(exc).__name__}: {exc}")
        else:
            # 不能确认完整观测：只记录尽力枚举，不把空列表当干净。
            fallback = [pid for pid in _descendant_tree(child.pid) if pid != child.pid]
            residual_before = fallback
            residual_after = fallback
            process_observation = "unknown"
        job.close()

        if child_result_json is not None:
            child_result, child_error = _read_json(child_result_json)
        else:
            child_error = "not_configured"

        cleanup_complete, cleanup_report_errors, cleanup_data, cleanup_error = _cleanup_report_state(
            cleanup_report_json
        )
        child_cleanup_errors = (
            child_result.get("cleanup_errors")
            if isinstance(child_result, dict) and isinstance(child_result.get("cleanup_errors"), list)
            else []
        )
        if cleanup_error is not None:
            cleanup_errors.append(f"cleanup_report:{cleanup_error}")
        if not cleanup_complete:
            cleanup_errors.append("cleanup_check_incomplete")
        cleanup_errors.extend(str(e) for e in cleanup_report_errors)
        cleanup_errors.extend(str(e) for e in child_cleanup_errors)
        if (
            cleanup_data is not None
            and child_result is not None
            and cleanup_data.get("errors") != child_result.get("cleanup_errors")
        ):
            cleanup_errors.append("cleanup_report_child_mismatch")

        if not timed_out:
            validation_ok, validation_reasons = _validate_child(
                child_result,
                child_error,
                run_id=run_id,
                snapshot=snapshot,
                expected_mode=exit_mode,
                child_exit_code=child_exit_code,
            )
        else:
            validation_ok = False
            validation_reasons = ["timeout"]

        after_snapshot = compute_source_snapshot_details(Path.cwd())
        snapshot_changed = after_snapshot.value != snapshot
        if snapshot_changed:
            validation_reasons.append("source_snapshot_changed_during_run")
        if not snapshot_detail.verified:
            validation_reasons.append("source_snapshot_unverified")
        if validation_reasons:
            validation_ok = False

        pytest_exit_code = None
        actual_mode = None
        if isinstance(child_result, dict):
            raw_pytest = child_result.get("pytest_exit_code")
            pytest_exit_code = raw_pytest if isinstance(raw_pytest, int) else None
            actual_mode = child_result.get("exit_mode")

        clean = bool(
            exit_mode == "clean"
            and validation_ok
            and not timed_out
            and child_exit_code == 0
            and pytest_exit_code == 0
            and snapshot_detail.verified
            and process_observation == "job"
            and not residual_before
            and not residual_after
            and cleanup_complete
            and not cleanup_errors
            and not validation_reasons
        )

        final: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "source_snapshot": snapshot,
            "source_snapshot_verified": snapshot_detail.verified,
            "source_snapshot_errors": snapshot_detail.errors,
            "source_snapshot_after": after_snapshot.value,
            "source_snapshot_changed": snapshot_changed,
            "exit_mode": exit_mode,
            "actual_exit_mode": actual_mode,
            "bypass_finalization": exit_mode == "bypass",
            "pytest_exit_code": pytest_exit_code,
            "child_exit_code": child_exit_code,
            "clean_exit_verified": clean,
            "timed_out": timed_out,
            "process_observation": process_observation,
            "residual_processes": residual_before,
            "residual_processes_after_cleanup": residual_after,
            "cleanup_check_complete": cleanup_complete,
            "cleanup_errors": cleanup_errors,
            "validation": {
                "ok": validation_ok,
                "reasons": validation_reasons,
                "child_result": str(child_result_json) if child_result_json else None,
                "child_error": child_error,
            },
            "child_result": child_result,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "log": str(log_path),
            "command": cmd,
        }
        if result_json is not None:
            try:
                _atomic_write_json(result_json, final)
            except Exception as exc:
                cleanup_errors.append(f"write_result_failed: {type(exc).__name__}: {exc}")
                print(f">>> 无法写入统一结果 {result_json}: {exc}", file=sys.stderr, flush=True)
                return EXIT_RESULT_INVALID

        if timed_out:
            return 124
        if startup_failure:
            return EXIT_RESULT_INVALID
        if child_exit_code != 0:
            return child_exit_code
        if not validation_ok:
            return EXIT_RESULT_INVALID
        if exit_mode == "clean" and not clean:
            return EXIT_CLEAN_NOT_VERIFIED
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=float, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--result-json", default=None)
    ap.add_argument("--child-result-json", default=None)
    ap.add_argument("--cleanup-report-json", default=None)
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
        child_result_json=Path(args.child_result_json).resolve() if args.child_result_json else None,
        cleanup_report_json=(
            Path(args.cleanup_report_json).resolve() if args.cleanup_report_json else None
        ),
        exit_mode=args.exit_mode,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
