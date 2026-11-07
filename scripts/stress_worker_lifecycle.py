"""Long-run worker lifecycle pressure diagnostic (manual, not in the default suite).

Reuses one WorkerProcess across many start/stop cycles and executes real GDAL
translate tasks at a fixed interval. Records parent handle/thread/memory growth,
worker PID/generation per cycle, and clean shutdown convergence.

Usage:
    python scripts/stress_worker_lifecycle.py --cycles 40 --task-every 3

Exit code 0 only when every task succeeds, no worker threads/processes remain,
and handle growth stays within the recorded bound.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from spectra_workbench.domain.models import (  # noqa: E402
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.workers.process import WorkerProcess  # noqa: E402


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _handle_count() -> int:
    count = ctypes.c_uint32(0)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return int(count.value)


def _memory_bytes() -> dict[str, int]:
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        ctypes.sizeof(counters),
    )
    if not ok:
        return {"working_set": 0, "private": 0}
    return {
        "working_set": int(counters.WorkingSetSize),
        "private": int(counters.PrivateUsage),
    }


def _worker_threads(worker_id: str) -> list[str]:
    import threading

    prefix = f"worker-{worker_id}-"
    return sorted(t.name for t in threading.enumerate() if t.name.startswith(prefix))


def _make_spec(source: Path, target: Path) -> TaskSpec:
    return TaskSpec(
        task_id=f"stress-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal",
            runtime_id="gdal-qgis",
            expected_version="3.13.3",
        ),
        project_root=str(PROJECT_ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id=f"in-{uuid.uuid4().hex[:6]}",
                kind=DatasetKind.raster,
                uri=source.resolve().as_uri(),
            )
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=target.resolve().as_uri(),
                overwrite=True,
            )
        ],
        parameters={"format": "GTiff"},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--task-every", type=int, default=3)
    parser.add_argument(
        "--out",
        default=str(PROJECT_ROOT / "docs" / "progress" / "runs" / "worker_long_run.json"),
    )
    args = parser.parse_args()

    source = PROJECT_ROOT / "tests" / "fixtures" / "data" / "a01_multiband_4b_8x8.hdr"
    if not source.exists():
        raise SystemExit(f"缺少测试源数据：{source}")
    work_dir = PROJECT_ROOT / "work" / "stress_worker"
    work_dir.mkdir(parents=True, exist_ok=True)

    worker_id = f"stress-{uuid.uuid4().hex[:8]}"
    worker = WorkerProcess(provider_id="gdal", worker_id=worker_id, project_root=PROJECT_ROOT)
    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    tasks_ok = 0
    started = datetime.datetime.now(datetime.UTC)
    started_monotonic = time.monotonic()
    handle_before = _handle_count()
    mem_before = _memory_bytes()
    threads_before = len(_worker_threads(worker_id))
    max_handle_growth = 0

    try:
        for cycle in range(1, args.cycles + 1):
            target = work_dir / f"cycle_{cycle:03d}.tif"
            try:
                worker.start(timeout=20.0)
                assert worker.state.value == "ready"
                pid = worker._proc.pid if worker._proc is not None else None
                info = worker.probe_dataset(source, timeout=20.0)
                assert info.get("width") == 8 and info.get("count") == 4

                task_ok: bool | None = None
                if cycle % args.task_every == 0:
                    result = worker.execute_task(
                        _make_spec(source, target),
                        timeout=60.0,
                    )
                    task_ok = result.status is TaskStatus.succeeded
                    if not task_ok:
                        failures.append(
                            f"cycle {cycle}: task failed {result.error}"
                        )
                    else:
                        tasks_ok += 1
                        if not target.exists():
                            failures.append(f"cycle {cycle}: 目标文件未生成")
                worker.stop(timeout=10.0)
                handles = _handle_count()
                max_handle_growth = max(max_handle_growth, handles - handle_before)
                samples.append(
                    {
                        "cycle": cycle,
                        "pid": pid,
                        "generation": worker.generation,
                        "handles": handles,
                        "threads": len(_worker_threads(worker_id)),
                        "memory": _memory_bytes(),
                        "task_ok": task_ok,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - diagnostic records the failure
                failures.append(f"cycle {cycle}: {type(exc).__name__}: {exc}")
                break
            finally:
                target.unlink(missing_ok=True)
    finally:
        worker.kill()
        mem_after = _memory_bytes()
        handle_after = _handle_count()
        threads_after = _worker_threads(worker_id)

    duration = time.monotonic() - started_monotonic
    report = {
        "schema_version": "1.0",
        "kind": "worker_lifecycle_long_run",
        "started_at": started.isoformat(),
        "finished_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "duration_seconds": round(duration, 3),
        "cycles_requested": args.cycles,
        "cycles_completed": len(samples),
        "task_every": args.task_every,
        "tasks_succeeded": tasks_ok,
        "worker_id": worker_id,
        "handle_before": handle_before,
        "handle_after": handle_after,
        "handle_growth": handle_after - handle_before,
        "max_handle_growth": max_handle_growth,
        "threads_before": threads_before,
        "threads_after": len(threads_after),
        "worker_threads_after": threads_after,
        "memory_before": mem_before,
        "memory_after": mem_after,
        "memory_private_growth": mem_after["private"] - mem_before["private"],
        "failures": failures,
        "samples": samples,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: report[k] for k in (
        "cycles_completed",
        "tasks_succeeded",
        "handle_growth",
        "max_handle_growth",
        "threads_after",
        "memory_private_growth",
        "failures",
    )}, ensure_ascii=False))
    print(f"report={out_path}")

    if failures:
        return 1
    if len(samples) != args.cycles:
        return 1
    if threads_after:
        return 1
    if handle_after - handle_before > 16:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
