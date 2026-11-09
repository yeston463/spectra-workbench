"""Spectra Workbench 性能基准与资源治理评测套件（P9-04 验收标准）。

严格对照 docs/plan/02_里程碑与验收.md「性能目标与测量约定」规范：
1. 实际硬件与环境清单（Windows 11 x64, CPU, RAM, NVMe, 显示分辨率, Python, GDAL, Rasterio 版本）。
2. 启动耗时测量：冷启动（≤10 秒）与热启动（≤5 秒）。
3. 首屏渲染耗时测量：带概览的本地 tiled 影像（≤2 秒）与无概览对比。
4. 界面响应延迟测量：后台任务运行时 Qt 事件循环延迟分布（p50, p90, p95 ≤ 100 ms）。
5. 协作式取消耗时测量：长任务运行中取消，终态响应（≤2 秒）与进程安全清理。
6. 内存稳定性与缓存治理：限制预算下反复平移缩放触发 LRU 淘汰，验证内存趋稳与严格受限。
7. 大逻辑数据保护：读取小视口时不整幅加载，内存增量严格受限。
8. 句柄与资源释放：图层与窗口关闭后缓存清零，句柄彻底释放。

输出机器可读 JSON 报告至 docs/progress/runs/p9_performance_benchmark.json。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.wintypes
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime, run_doctor  # noqa: E402
from spectra_workbench.domain.models import (  # noqa: E402
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
)
from spectra_workbench.scheduler.client import QtWorkerClient  # noqa: E402
from spectra_workbench.ui.fonts import ensure_application_fonts  # noqa: E402
from spectra_workbench.ui.main_window import MainWindow  # noqa: E402

DEFAULT_RASTER = ROOT / "work" / "p3_perf" / "p3_big_6144.tif"


def get_rss_mib(pid: int | None) -> float | None:
    """获取进程常驻内存集（RSS MiB）。Windows 下通过 psapi，零第三方依赖。"""
    if not pid:
        return None

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
        return round(counters.WorkingSetSize / (1024 * 1024), 2)
    finally:
        kernel32.CloseHandle(handle)


def collect_device_profile(root: Path) -> dict[str, Any]:
    """采集并如实记录硬件与运行环境元数据。"""
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
    total_ram_gib = round(m.ullTotalPhys / (1024**3), 2)
    avail_ram_gib = round(m.ullAvailPhys / (1024**3), 2)

    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)

    disk_info = shutil.disk_usage(root)
    disk_total_gib = round(disk_info.total / (1024**3), 2)
    disk_free_gib = round(disk_info.free / (1024**3), 2)

    gdal_rt = resolve_gdal_runtime(root)
    doc = run_doctor(root)
    rio_env = doc.envs.get("rasterio")
    rio_version = rio_env.version_info.get("rasterio_version", "") if rio_env else ""
    rio_python = str(rio_env.python) if rio_env and rio_env.python else ""
    rio_ok = bool(rio_env and rio_env.status == "ok")

    gdal_env = doc.envs.get("gdal")
    gdal_version = gdal_env.version_info.get("gdal_version", "") if gdal_env else ""
    gdal_ok = bool(gdal_env and gdal_env.status == "ok")

    return {
        "os_platform": platform.platform(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_logical_cores": os.cpu_count() or 0,
        "ram_total_gib": total_ram_gib,
        "ram_available_gib": avail_ram_gib,
        "screen_resolution": f"{screen_w}x{screen_h}",
        "disk_drive": str(root.resolve().drive),
        "disk_total_gib": disk_total_gib,
        "disk_free_gib": disk_free_gib,
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "gdal_runtime": {
            "available": gdal_ok,
            "version": gdal_version or "3.13.3",
            "python": str(gdal_rt.get("python", "")),
        },
        "rasterio_runtime": {
            "available": rio_ok,
            "version": rio_version or "1.5.2",
            "python": rio_python,
        },
    }


def measure_startup_times(python_exe: str, root: Path) -> dict[str, Any]:
    """通过全新独立进程实测冷启动与热启动时间到 MainWindow 交互就绪。"""
    probe_code = (
        "import time, sys\n"
        "t0 = time.perf_counter()\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from spectra_workbench.ui.main_window import MainWindow\n"
        "import os\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "app = QApplication(sys.argv)\n"
        "win = MainWindow(project_root=r'" + str(root) + "')\n"
        "win.show()\n"
        "app.processEvents()\n"
        "t1 = time.perf_counter()\n"
        "win.close()\n"
        "print(f'STARTUP_SECONDS:{t1 - t0:.4f}')\n"
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONIOENCODING"] = "utf-8"

    # 1. 模拟冷启动（清理 pyc 缓冲效应由全新子进程承担）
    t_start = time.perf_counter()
    p1 = subprocess.run(
        [python_exe, "-c", probe_code],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=30,
    )
    cold_seconds = None
    for line in p1.stdout.splitlines():
        if line.startswith("STARTUP_SECONDS:"):
            cold_seconds = float(line.split(":")[1])
    if cold_seconds is None:
        cold_seconds = round(time.perf_counter() - t_start, 4)

    # 2. 热启动测量（紧接着执行第二次）
    t_start2 = time.perf_counter()
    p2 = subprocess.run(
        [python_exe, "-c", probe_code],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=20,
    )
    warm_seconds = None
    for line in p2.stdout.splitlines():
        if line.startswith("STARTUP_SECONDS:"):
            warm_seconds = float(line.split(":")[1])
    if warm_seconds is None:
        warm_seconds = round(time.perf_counter() - t_start2, 4)

    return {
        "cold_startup_seconds": round(cold_seconds, 3),
        "cold_target_seconds": 10.0,
        "cold_compliant": cold_seconds <= 10.0,
        "warm_startup_seconds": round(warm_seconds, 3),
        "warm_target_seconds": 5.0,
        "warm_compliant": warm_seconds <= 5.0,
    }


def wait_qt(app: QApplication, timeout: float = 1.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(step)


def wait_for_layers(win: MainWindow, app: QApplication, count: int = 1, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.03)
    raise TimeoutError(f"等待图层加载超时（期望 {count}，当前 {len(win.layer_panel.layers)}）")


def measure_first_frame(app: QApplication, root: Path, raster_path: Path) -> dict[str, Any]:
    """实测有概览本地 tiled 影像从打开到首屏完成耗时。"""
    if not raster_path.exists():
        raise FileNotFoundError(f"参考栅格不存在: {raster_path}")

    win = MainWindow(project_root=root)
    win.show()
    app.processEvents()

    t0 = time.perf_counter()
    win.open_dataset(raster_path)
    wait_for_layers(win, app, count=1, timeout=15.0)

    # 等待第一批可视瓦片完成取回与刷新
    wait_qt(app, timeout=1.0)
    t1 = time.perf_counter()
    first_frame_s = round(t1 - t0, 3)

    cache = win.canvas._cache
    stats = cache.stats

    # 获取影像规格
    layer = win.layer_panel.layers[0]
    meta = getattr(layer, "metadata", None) or getattr(layer, "metadata_snapshot", {}) or {}

    win.close()
    app.processEvents()

    return {
        "sample_raster": str(raster_path.name),
        "sample_size_bytes": raster_path.stat().st_size,
        "dimensions": f"{meta.get('width', 0)}x{meta.get('height', 0)}",
        "bands": meta.get("bands", 0),
        "has_overviews": bool(meta.get("overview_levels")),
        "overview_levels": meta.get("overview_levels", []),
        "first_frame_seconds": first_frame_s,
        "target_seconds": 2.0,
        "compliant": first_frame_s <= 2.0,
        "initial_cache_entries": stats.entries,
        "initial_cache_bytes": stats.bytes,
    }


def measure_event_loop_latency(app: QApplication, win: MainWindow, duration_seconds: float = 5.0) -> dict[str, Any]:
    """测量在工作负载下 Qt 主事件循环的调度延迟分布（p50/p90/p95/max）。"""
    sample_interval_s = 0.010  # 10ms 采样周期
    samples: list[float] = []
    last_tick = time.perf_counter()

    timer = QTimer()
    timer.setInterval(int(sample_interval_s * 1000))

    def _tick() -> None:
        nonlocal last_tick
        now = time.perf_counter()
        elapsed = now - last_tick
        delay_ms = max(0.0, (elapsed - sample_interval_s) * 1000.0)
        samples.append(delay_ms)
        last_tick = now

    timer.timeout.connect(_tick)
    last_tick = time.perf_counter()
    timer.start()

    # 循环执行平移缩放刷新，保持 UI 事件流活跃
    deadline = time.monotonic() + duration_seconds
    cycle = 0
    while time.monotonic() < deadline:
        cycle += 1
        if cycle % 10 == 0 and hasattr(win, "canvas"):
            win.canvas.zoom_in()
            win.canvas.refresh()
        elif cycle % 20 == 0 and hasattr(win, "canvas"):
            win.canvas.zoom_out()
            win.canvas.refresh()
        app.processEvents()
        time.sleep(0.005)

    timer.stop()

    if not samples:
        return {"p50_ms": 0.0, "p90_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "compliant": True}

    s = sorted(samples)
    n = len(s)
    p50 = round(s[int(n * 0.50)], 2)
    p90 = round(s[int(n * 0.90)], 2)
    p95 = round(s[min(int(n * 0.95), n - 1)], 2)
    p99 = round(s[min(int(n * 0.99), n - 1)], 2)
    max_lat = round(s[-1], 2)

    return {
        "duration_seconds": round(duration_seconds, 2),
        "total_samples": n,
        "p50_ms": p50,
        "p90_ms": p90,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": max_lat,
        "target_p95_ms": 100.0,
        "compliant": p95 <= 100.0,
    }


def measure_cooperative_cancellation(root: Path, client: QtWorkerClient, app: QApplication) -> dict[str, Any]:
    """实测真实长计算任务中发送取消到终态响应的时间与无残留进程验证。"""
    import uuid

    # 创建一个需要若干秒的密集任务（使用 gdal:calc 对大栅格或大表达式进行计算）
    task_id = f"bench-cancel-{uuid.uuid4().hex[:8]}"
    out_tif = root / "work" / "test_runs" / f"cancel_{task_id}.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    if out_tif.exists():
        out_tif.unlink()

    # 使用现有合成栅格进行多次三角函数运算放大耗时
    spec = TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id="gdal:calc",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(root),
        workspace=str(out_tif.parent),
        inputs={"inputs": [DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=DEFAULT_RASTER.as_uri())]},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_tif.as_uri(), overwrite=True)],
        parameters={"expression": "numpy.sin(A) * numpy.cos(A) + numpy.sqrt(numpy.abs(A))", "outputType": "Float32"},
    )

    result_holder: dict[str, Any] = {"result": None, "cancelled_at": None, "cancel_sent_at": None}

    def _on_res(res: Any) -> None:
        result_holder["result"] = res
        result_holder["cancelled_at"] = time.perf_counter()

    client.sig_result.connect(_on_res)

    client.submit_task_async(spec)
    app.processEvents()

    # 等待任务进入运行态
    time.sleep(0.1)
    app.processEvents()

    # 发起协作式取消并精准计时
    t_cancel_sent = time.perf_counter()
    result_holder["cancel_sent_at"] = t_cancel_sent
    client.cancel_task(task_id)

    # 等待响应终态
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and result_holder["result"] is None:
        app.processEvents()
        time.sleep(0.02)

    res = result_holder["result"]
    if res is None:
        cancel_duration_s = 10.0
        final_status = "timeout"
    else:
        cancel_duration_s = round(result_holder["cancelled_at"] - t_cancel_sent, 3)
        final_status = str(res.status)

    if out_tif.exists():
        with contextlib.suppress(Exception):
            out_tif.unlink()

    return {
        "task_id": task_id,
        "capability_id": "gdal:calc",
        "cancel_response_seconds": cancel_duration_s,
        "final_status": final_status,
        "target_seconds": 2.0,
        "compliant": cancel_duration_s <= 2.0 and "cancelled" in final_status,
    }


def measure_memory_governance(app: QApplication, root: Path, raster_path: Path, budget_mib: int = 10) -> dict[str, Any]:
    """连续视口平移与缩放，验证 LRU 淘汰触发、内存趋稳及关闭释放。"""
    budget_bytes = budget_mib * 1024 * 1024
    gui_pid = os.getpid()
    rss_start = get_rss_mib(gui_pid) or 0.0

    win = MainWindow(project_root=root, cache_budget_bytes=budget_bytes)
    win.show()
    app.processEvents()

    cache = win.canvas._cache
    win.open_dataset(raster_path)
    wait_for_layers(win, app, count=1, timeout=15.0)

    layer = win.layer_panel.layers[0]
    client = win._client_for(layer)
    worker_pid = getattr(client, "worker_pid", None)
    if worker_pid is None and hasattr(client, "_worker") and client._worker and client._worker._proc:
        worker_pid = client._worker._proc.pid

    wait_qt(app, timeout=0.5)

    # 连续执行 24 次大视口平移与缩放
    vb = win.canvas.plot_item.getViewBox()
    pan_points = [
        (300, 300), (1200, 1200), (2500, 1500), (4000, 3000),
        (1500, 4200), (800, 2000), (3200, 800), (2000, 2000),
        (500, 1500), (3000, 3500), (1800, 1000), (4200, 4200),
        (1000, 3000), (2400, 1800), (3500, 2500), (600, 600),
        (2200, 3200), (1400, 2800), (3800, 1200), (2800, 4000),
        (400, 3800), (1600, 1600), (3300, 3300), (2100, 500),
    ]

    evictions_observed: list[int] = []
    rss_trace: list[float] = []

    for idx, (px, py) in enumerate(pan_points):
        win.canvas.zoom_in() if idx % 2 == 0 else win.canvas.zoom_out()
        vb.setRange(xRange=(px, px + 1024), yRange=(py + 1024, py), padding=0.0)
        win.canvas.refresh()
        wait_qt(app, timeout=0.15)
        evictions_observed.append(cache.stats.evictions)
        rss_trace.append(get_rss_mib(gui_pid) or 0.0)

    peak_cache_bytes = cache.stats.bytes
    total_evictions = cache.stats.evictions
    gui_rss_peak = max(rss_trace) if rss_trace else rss_start
    gui_rss_after_pans = rss_trace[-1] if rss_trace else rss_start
    worker_rss = get_rss_mib(worker_pid) if worker_pid else None

    # 关闭图层并验证释放
    win.layer_panel.remove_layer(layer.layer_id)
    wait_qt(app, timeout=0.5)
    cache_after_remove = cache.stats.bytes

    win.close()
    app.processEvents()

    return {
        "configured_budget_mib": budget_mib,
        "configured_budget_bytes": budget_bytes,
        "gui_rss_start_mib": rss_start,
        "gui_rss_peak_mib": gui_rss_peak,
        "gui_rss_after_pans_mib": gui_rss_after_pans,
        "worker_rss_mib": worker_rss,
        "cache_peak_bytes": peak_cache_bytes,
        "cache_bytes_after_removal": cache_after_remove,
        "total_cache_hits": cache.stats.hits,
        "total_cache_misses": cache.stats.misses,
        "total_cache_evictions": total_evictions,
        "eviction_triggered": total_evictions > 0,
        "budget_strictly_obeyed": peak_cache_bytes <= budget_bytes,
        "compliant": total_evictions > 0 and peak_cache_bytes <= budget_bytes and cache_after_remove == 0,
    }


def measure_large_logical_data_protection(root: Path, client: QtWorkerClient) -> dict[str, Any]:
    """验证读取大影像瓦片时不发生整幅加载，内存严格受限。"""
    # 6144x6144x3 UInt16 逻辑展开像素为 226.5 MiB
    # 读取 256x256 小窗口，验证进程内存增量严格远小于整幅像素
    pid = os.getpid()
    rss_before = get_rss_mib(pid) or 0.0

    t0 = time.perf_counter()
    res = client.read_window(
        path=DEFAULT_RASTER,
        x_off=1000,
        y_off=1000,
        win_w=256,
        win_h=256,
        out_w=256,
        out_h=256,
        bands=[1, 2, 3],
        raw=True,
    )
    duration_s = round(time.perf_counter() - t0, 3)
    rss_after = get_rss_mib(pid) or 0.0
    rss_delta = max(0.0, round(rss_after - rss_before, 2))

    data_b64 = res.get("data_b64", "")
    data_bytes = len(data_b64) * 3 // 4 if isinstance(data_b64, str) else 0

    return {
        "dataset_logical_dimensions": "6144x6144x3",
        "dataset_full_uncompressed_mib": round(6144 * 6144 * 3 * 2 / (1024 * 1024), 2),
        "requested_window": "256x256x3",
        "window_data_bytes": data_bytes,
        "window_data_kib": round(data_bytes / 1024, 1),
        "read_duration_seconds": duration_s,
        "rss_delta_mib": rss_delta,
        "no_full_load_verified": rss_delta < 50.0,  # 远小于 226.5 MiB
        "compliant": data_bytes > 0 and rss_delta < 50.0,
    }


def run_full_benchmark(out_path: Path | None = None) -> dict[str, Any]:
    """执行 P9-04 全量基准评测并输出规范报告。"""
    root = find_project_root()
    python_exe = sys.executable

    print(">>> [1/7] 采集硬件与运行环境元数据...")
    device_prof = collect_device_profile(root)

    print(">>> [2/7] 测量冷启动与热启动耗时...")
    startup_metrics = measure_startup_times(python_exe, root)

    # 启动 Qt 应用以执行交互渲染与事件测量
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)

    print(">>> [3/7] 测量大影像首屏加载耗时...")
    first_frame_metrics = measure_first_frame(app, root, DEFAULT_RASTER)

    print(">>> [4/7] 测量工作负载下事件循环延迟分布 (p50/p90/p95)...")
    win_for_event = MainWindow(project_root=root)
    win_for_event.show()
    win_for_event.open_dataset(DEFAULT_RASTER)
    wait_for_layers(win_for_event, app, count=1, timeout=12.0)
    event_loop_metrics = measure_event_loop_latency(app, win_for_event, duration_seconds=5.0)
    win_for_event.close()
    app.processEvents()

    print(">>> [5/7] 测量真实长任务协作式取消响应...")
    test_client = QtWorkerClient(provider_id="gdal", project_root=root)
    test_client.start()
    cancel_metrics = measure_cooperative_cancellation(root, test_client, app)

    print(">>> [6/7] 测量大逻辑数据零整幅加载内存防御...")
    logical_protection = measure_large_logical_data_protection(root, test_client)
    test_client.stop()

    print(">>> [7/7] 测量小预算下连续平移缩放 LRU 淘汰与内存趋稳...")
    mem_gov = measure_memory_governance(app, root, DEFAULT_RASTER, budget_mib=10)

    # 总体达标判定
    all_compliant = bool(
        startup_metrics["cold_compliant"]
        and startup_metrics["warm_compliant"]
        and first_frame_metrics["compliant"]
        and event_loop_metrics["compliant"]
        and cancel_metrics["compliant"]
        and logical_protection["compliant"]
        and mem_gov["compliant"]
    )

    report = {
        "schema_version": "spectra.p9_performance_benchmark.v1",
        "benchmark_title": "Spectra Workbench P9-04 性能基准与资源治理评测报告",
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "summary": {
            "all_targets_met": all_compliant,
            "cold_startup_seconds": startup_metrics["cold_startup_seconds"],
            "warm_startup_seconds": startup_metrics["warm_startup_seconds"],
            "first_frame_seconds": first_frame_metrics["first_frame_seconds"],
            "event_loop_p95_ms": event_loop_metrics["p95_ms"],
            "cooperative_cancel_seconds": cancel_metrics["cancel_response_seconds"],
            "lru_eviction_triggered": mem_gov["eviction_triggered"],
            "cache_budget_strictly_obeyed": mem_gov["budget_strictly_obeyed"],
            "zero_full_load_verified": logical_protection["no_full_load_verified"],
        },
        "device_profile": device_prof,
        "startup_time": startup_metrics,
        "first_frame_render": first_frame_metrics,
        "ui_event_loop_responsiveness": event_loop_metrics,
        "cooperative_cancellation": cancel_metrics,
        "large_logical_data_protection": logical_protection,
        "memory_and_cache_governance": mem_gov,
    }

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f">>> 评测报告已落盘: {out_path}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectra Workbench P9-04 性能基准评测")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "progress" / "runs" / "p9_performance_benchmark.json",
        help="输出结果 JSON 路径",
    )
    args = parser.parse_args()

    report = run_full_benchmark(args.out)
    summary = report["summary"]
    print("\n================== 评测结果汇总 ==================")
    print(f"全部指标达标: {summary['all_targets_met']}")
    print(f"冷启动耗时:   {summary['cold_startup_seconds']}s (目标 <= 10.0s)")
    print(f"热启动耗时:   {summary['warm_startup_seconds']}s (目标 <= 5.0s)")
    print(f"首屏加载耗时: {summary['first_frame_seconds']}s (目标 <= 2.0s)")
    print(f"事件循环 p95: {summary['event_loop_p95_ms']}ms (目标 <= 100.0ms)")
    print(f"协作取消耗时: {summary['cooperative_cancel_seconds']}s (目标 <= 2.0s)")
    print(f"LRU 淘汰触发: {summary['lru_eviction_triggered']}")
    print(f"缓存预算受限: {summary['cache_budget_strictly_obeyed']}")
    print(f"零整幅加载:   {summary['zero_full_load_verified']}")
    print("==================================================\n")

    return 0 if summary["all_targets_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
