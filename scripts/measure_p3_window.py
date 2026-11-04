"""P3-03 大影像读取的窗口请求与内存测量（可复现）。

用法（由 UI 环境执行；栅格本身交给 GDAL 运行时生成）：
    .venv/Scripts/python.exe scripts/measure_p3_window.py [--size 6144] [--requests 36]
                                                           [--out docs/progress/runs/p3_perf.json]

产出 JSON 记录：栅格规格与 sha256、每次窗口请求的参数/耗时/字节数、概览是否被选用、
TileCache 在默认预算下的命中与逐出、GUI 与 worker 进程的常驻内存、spill 文件数量变化，
以及全量统计在超过上限时的拒绝行为。

只做读取与测量：不写任何源数据。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime  # noqa: E402
from spectra_workbench.rendering.cache import TileCache, TileKey, default_budget_bytes  # noqa: E402
from spectra_workbench.workers.process import WorkerProcess  # noqa: E402

RASTER_NAME = "p3_big_6144.tif"


def rss_mib(pid: int) -> float | None:
    """进程常驻集（MiB）。Windows 下用 psapi，无第三方依赖。"""

    class Counter(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1004, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
    if not handle:
        return None
    try:
        psapi = ctypes.windll.psapi
        counters = Counter()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / 2**20
    finally:
        kernel32.CloseHandle(handle)


def available_mem_mib() -> float:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = MemoryStatus()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 2**20


def build_raster(root: Path, size: int, bands: int) -> dict[str, Any]:
    """在 GDAL 运行时里生成大合成栅格（分块 COG 风格 + 概览）。"""
    info = resolve_gdal_runtime(root)
    py = Path(str(info.get("python", "")))
    if not py.exists():
        raise RuntimeError(f"GDAL 运行时不可用：{info.get('reason')}")
    script = Path(__file__).with_name("measure_p3_raster_build.py")
    out = root / "work" / "p3_perf" / RASTER_NAME
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(root / "src")
    for k, v in (info.get("env") or {}).items():
        env[str(k)] = str(v)
    proc = subprocess.run(
        [str(py), str(script), "--out", str(out), "--size", str(size), "--bands", str(bands)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"栅格生成失败 (code={proc.returncode}):\n{proc.stderr[-1500:]}")
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    if not tail:
        raise RuntimeError(f"栅格生成器未返回清单：\n{proc.stdout[-800:]}")
    spec = json.loads(tail[-1])
    spec["sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
    return spec


def request_plan(size: int, count: int) -> list[dict[str, int]]:
    """确定性的平移/缩放序列：既有 1:1 窗口，也有必须走概览的下采样窗口。"""
    plan: list[dict[str, int]] = []
    step = max(1, size // 8)
    for i in range(count):
        cycle = i % 6
        if cycle < 3:  # 1:1 浏览（应不触发概览）
            win = 1024
            out = 1024
        elif cycle == 3:  # 缩到 1/2（应触发概览）
            win = 2048
            out = 1024
        elif cycle == 4:  # 缩到 1/8
            win = 4096
            out = 512
        else:  # 全幅级别
            win = size
            out = 512
        x = (i * step) % max(1, size - win + 1)
        y = ((i * 3) * step) % max(1, size - win + 1)
        plan.append({"x_off": x, "y_off": y, "win_w": win, "win_h": win, "out_w": out, "out_h": out})
    return plan


def np_dtype_size(name: str) -> int:
    return {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "float32": 4, "uint32": 4, "float64": 8}[
        str(name).lower()
    ]


def spill_files() -> int:
    root = Path(tempfile.gettempdir()) / "spectra_workbench_spill"
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def measure(args: argparse.Namespace) -> dict[str, Any]:
    root = find_project_root()
    raster = root / "work" / "p3_perf" / RASTER_NAME
    built: dict[str, Any] | None = None
    if args.rebuild or not raster.exists():
        built = build_raster(root, args.size, args.bands)
    if not raster.exists():
        raise RuntimeError(f"栅格不存在：{raster}")
    if built is None:
        h = hashlib.sha256()
        with raster.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        built = {"reused": True, "sha256": h.hexdigest()}

    proc = WorkerProcess(provider_id="gdal", project_root=root)
    proc.start()
    results: list[dict[str, Any]] = []
    cache = TileCache(budget_bytes=default_budget_bytes())
    bytes_over_wire = 0
    spill_before = spill_files()
    gui_rss_before = rss_mib(os.getpid())
    worker_rss_before = rss_mib(proc._proc.pid) if proc._proc else None  # noqa: SLF001
    worker_rss_after_full: float | None = None
    refused_full_stats: str | None = None
    full_ok: dict[str, Any] | None = None
    full_ms: float | None = None
    meta: dict[str, Any] = {}
    try:
        t0 = time.perf_counter()
        meta = proc.probe_dataset(raster, timeout=120.0)
        probe_ms = round((time.perf_counter() - t0) * 1000, 1)

        for item in request_plan(args.size, args.requests):
            bands = list(range(1, min(3, int(meta["count"])) + 1))
            key = TileKey(
                source=str(raster),
                bands=tuple(bands),
                x_off=item["x_off"],
                y_off=item["y_off"],
                win_w=item["win_w"],
                win_h=item["win_h"],
                out_w=item["out_w"],
                out_h=item["out_h"],
                overview=-1,
            )
            cached = cache.get(key) is not None
            t1 = time.perf_counter()
            payload = proc.read_window(
                raster,
                x_off=item["x_off"],
                y_off=item["y_off"],
                win_w=item["win_w"],
                win_h=item["win_h"],
                out_w=item["out_w"],
                out_h=item["out_h"],
                bands=bands,
                raw=True,
                timeout=120.0,
            )
            ms = round((time.perf_counter() - t1) * 1000, 1)
            wire = len(base64.b64decode(payload["data_b64"])) + len(
                base64.b64decode(payload["valid_bits_b64"])
            )
            bytes_over_wire += wire
            # 与界面缓存同一种存放形式（整份 JSON/base64 文本），字节数才是真实占用
            blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            cache.put(key, blob)
            nb = len(bands)
            oh, ow = int(payload["shape"][1]), int(payload["shape"][2])
            decoded_window = nb * oh * ow * np_dtype_size(payload["numpy_dtype"])
            results.append(
                {
                    **item,
                    "bands": bands,
                    "ms": ms,
                    "wire_bytes": wire,
                    "cache_json_bytes": len(blob),
                    "decoded_window_bytes": decoded_window,
                    "overview_used": payload.get("overview_used"),
                    "from_cache": cached,
                    "shape": payload.get("shape"),
                    "numpy_dtype": payload.get("numpy_dtype"),
                }
            )
            del payload, blob

        t2 = time.perf_counter()
        sampled = proc.compute_stats(raster, mode="sampled", max_samples=200_000, timeout=120.0)
        sampled_ms = round((time.perf_counter() - t2) * 1000, 1)

        pixels_per_band = int(meta["width"]) * int(meta["height"])
        # 上限高于单波段像元数时，全量统计必须真的算完；低于时必须拒绝，而不是偷偷抽样
        t3 = time.perf_counter()
        full_ok = proc.compute_stats(
            raster, mode="full", full_limit=pixels_per_band + 1, timeout=300.0
        )
        full_ms = round((time.perf_counter() - t3) * 1000, 1)
        worker_rss_after_full = rss_mib(proc._proc.pid) if proc._proc else None
        try:
            proc.compute_stats(
                raster, mode="full", full_limit=pixels_per_band // 10, timeout=300.0
            )
        except Exception as exc:  # 期望：超过上限必须拒绝而不是静默降级为抽样
            refused_full_stats = str(exc)
    finally:
        gui_rss_after = rss_mib(os.getpid())
        worker_rss_after = rss_mib(proc._proc.pid) if proc._proc else None
        spill_mid = spill_files()
        proc.stop(timeout=10.0)
        spill_after_stop = spill_files()

    times = [r["ms"] for r in results]

    def _r(v: float | None) -> float | None:
        return None if v is None else round(v, 1)

    return {
        "schema": "spectra.p3_perf.v1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": " ".join([Path(sys.executable).name, *[sys.argv[0]], *sys.argv[1:]]),
        "host": {
            "available_mem_mib_before": round(available_mem_mib(), 1),
            "pid_gui": os.getpid(),
        },
        "raster": {
            "path": str(raster),
            "size_bytes": raster.stat().st_size,
            "width": meta.get("width"),
            "height": meta.get("height"),
            "bands": meta.get("count"),
            "driver": meta.get("driver"),
            "crs_epsg": meta.get("crs_epsg"),
            "overview_levels": meta.get("overview_levels"),
            "built": built,
        },
        "probe": {"ms": probe_ms, "count": meta.get("count"), "overview_levels": meta.get("overview_levels")},
        "cache_budget": {
            "budget_bytes": cache.budget_bytes,
            "budget_source": "default_budget_bytes() (10% 可用内存, 上限 512MiB, 可用 SPECTRA_TILE_CACHE_MIB 覆盖)",
            "bytes_after": cache.stats.bytes,
            "hits": cache.stats.hits,
            "misses": cache.stats.misses,
            "evictions": cache.stats.evictions,
            "entries": cache.stats.entries,
        },
        "window_requests": {
            "count": len(results),
            "wire_bytes_total": bytes_over_wire,
            "wire_bytes_max": max((r["wire_bytes"] for r in results), default=0),
            "cache_json_bytes_total": sum(r["cache_json_bytes"] for r in results),
            "decoded_window_bytes_max": max((r["decoded_window_bytes"] for r in results), default=0),
            "ms_min": min(times, default=None),
            "ms_median": round(statistics.median(times), 1) if times else None,
            "ms_p95": round(sorted(times)[max(0, int(len(times) * 0.95) - 1)], 1) if times else None,
            "ms_max": max(times, default=None),
            "overview_used_counts": _count_overviews(results),
            "per_request": results,
        },
        "stats": {
            "sampled_ms": sampled_ms,
            "sampled_source": sampled.get("source"),
            "sampled_stride": sampled.get("stride"),
            "full_ms": full_ms,
            "full_source": (full_ok or {}).get("source"),
            "full_mode": (full_ok or {}).get("mode"),
            "full_pixels_per_band": (int(meta.get("width") or 0) * int(meta.get("height") or 0)) or None,
            "full_refused_as": refused_full_stats,
        },
        "memory_mib": {
            "note": "gui_* 是本测量脚本进程（界面读取路径的无界面替身：不含 Qt 绘制与显示数组）；worker_* 是 GDAL 子进程",
            "gui_before": _r(gui_rss_before),
            "gui_after": _r(gui_rss_after),
            "worker_before": _r(worker_rss_before),
            "worker_after_full_stats": _r(worker_rss_after_full),
            "worker_after": _r(worker_rss_after),
        },
        "spill_files": {
            "before": spill_before,
            "after_requests": spill_mid,
            "after_worker_stop": spill_after_stop,
        },
    }


def _count_overviews(results: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        key = str(r.get("overview_used"))
        out[key] = out.get(key, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=6144)
    ap.add_argument("--bands", type=int, default=3)
    ap.add_argument("--requests", type=int, default=36)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--out", default="docs/progress/runs/p3_perf.json")
    args = ap.parse_args()

    report = measure(args)
    dest = Path(args.out)
    if not dest.is_absolute():
        dest = find_project_root() / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    w = report["window_requests"]
    print(f"栅格 {report['raster']['path']} ({report['raster']['size_bytes'] / 2**20:.1f} MiB)")
    print(
        f"窗口请求 {w['count']} 次 | 中位数 {w['ms_median']} ms | p95 {w['ms_p95']} ms | "
        f"最大 {w['ms_max']} ms | 传输 {w['wire_bytes_total'] / 2**20:.1f} MiB"
    )
    print(f"缓存预算 {report['cache_budget']['budget_bytes'] / 2**20:.0f} MiB | "
          f"命中 {report['cache_budget']['hits']} 逐出 {report['cache_budget']['evictions']}")
    print(f"内存 MiB GUI {report['memory_mib']['gui_before']} -> {report['memory_mib']['gui_after']} | "
          f"worker {report['memory_mib']['worker_before']} -> "
          f"{report['memory_mib']['worker_after']} (全量统计后 "
          f"{report['memory_mib']['worker_after_full_stats']})")
    s = report["stats"]
    print(f"统计 抽样 {s['sampled_ms']} ms(source={s['sampled_source']}) | "
          f"全量 {s['full_ms']} ms(source={s['full_mode']}/{s['full_source']}) | "
          f"超上限拒绝: {s['full_refused_as']}")
    print(f"spill 文件 before/after/stop: {report['spill_files']}")
    print(f"报告写入 {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
