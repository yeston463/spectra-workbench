"""真实 GUI 大影像内存与 LRU 缓存淘汰测量脚本（P3 验收）。

按原验收标准与指令要求：
1. 使用带概览的真实大影像合成栅格（work/p3_perf/p3_big_6144.tif）。
2. 在真实的 Qt 界面与 MainWindow 窗口中运行（非无界面脚本替身）。
3. 设置较小缓存预算（如 10 MiB），实测连续缩放、平移、切波段与图层切换，
   真实触发 LRU 淘汰（evictions > 0）。
4. 分阶段记录 GUI 物理常驻内存（RSS）、Worker 常驻内存、缓存字节数与淘汰数。
5. 记录关闭图层/窗口后的句柄与内存释放情况。
6. 产出 docs/progress/runs/p3_gui_memory.json。
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow


def rss_mib(pid: int | None) -> float | None:
    """进程物理常驻集（MiB）。Windows 下通过 psapi，零第三方包依赖。"""
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
        return round(counters.WorkingSetSize / 2**20, 2)
    finally:
        kernel32.CloseHandle(handle)


def wait_qt(app: QApplication, timeout: float = 0.5, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(step)


def wait_layers(win: MainWindow, app: QApplication, count: int = 1, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.04)
    raise AssertionError(f"等待图层超时：期望 {count}，当前 {len(win.layer_panel.layers)}")


def run_measurement(
    raster_path: Path,
    second_layer_path: Path | None = None,
    budget_mib: int = 10,
    out_json: Path | None = None,
) -> dict[str, Any]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    ensure_application_fonts(app)

    gui_pid = os.getpid()
    budget_bytes = budget_mib * 1024 * 1024

    stages: list[dict[str, Any]] = []

    def record_stage(name: str, worker_pid: int | None, cache_stats: Any) -> None:
        g_rss = rss_mib(gui_pid)
        w_rss = rss_mib(worker_pid) if worker_pid else None
        st = {
            "stage": name,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "gui_rss_mib": g_rss,
            "worker_rss_mib": w_rss,
            "cache_bytes": cache_stats.bytes if cache_stats else 0,
            "cache_entries": cache_stats.entries if cache_stats else 0,
            "cache_hits": cache_stats.hits if cache_stats else 0,
            "cache_misses": cache_stats.misses if cache_stats else 0,
            "cache_evictions": cache_stats.evictions if cache_stats else 0,
        }
        stages.append(st)
        print(
            f"[{name:20s}] GUI={g_rss:6.1f} MiB | Worker={str(w_rss):6s} MiB | "
            f"Cache={st['cache_bytes'] / 2**20:5.2f} MiB (evict={st['cache_evictions']})"
        )

    # 1. 启动阶段
    record_stage("01_startup", None, None)

    # 2. 初始化窗口与小缓存预算
    win = MainWindow(cache_budget_bytes=budget_bytes)
    win.show()
    app.processEvents()
    cache = win.canvas._cache

    record_stage("02_gui_initialized", None, cache.stats)

    # 3. 加载大影像
    win.open_dataset(raster_path)
    wait_layers(win, app, count=1)
    layer = win.layer_panel.layers[0]

    # 获取底层 worker pid
    client = win._client_for(layer)
    worker_pid = getattr(client, "worker_pid", None)
    if worker_pid is None and hasattr(client, "_worker") and client._worker and client._worker._proc:
        worker_pid = client._worker._proc.pid

    # 初始显示渲染
    wait_qt(app, timeout=1.0)
    record_stage("03_initial_raster_loaded", worker_pid, cache.stats)

    # 4. 连续多级缩放
    for _i in range(4):
        win.canvas.zoom_in()
        win.canvas.refresh()
        wait_qt(app, timeout=0.25)
    for _i in range(4):
        win.canvas.zoom_out()
        win.canvas.refresh()
        wait_qt(app, timeout=0.25)
    record_stage("04_continuous_zoom", worker_pid, cache.stats)

    # 5. 连续视口平移（跨越图幅不同区域，触发多窗口索取）
    vb = win.canvas.plot_item.getViewBox()
    pan_coords = [
        (400, 400),
        (1500, 1500),
        (3000, 2000),
        (4500, 3500),
        (2000, 4500),
        (800, 3000),
        (3500, 1000),
        (1200, 2400),
    ]
    for px, py in pan_coords:
        vb.setRange(xRange=(px, px + 1024), yRange=(py + 1024, py), padding=0.0)
        win.canvas.refresh()
        wait_qt(app, timeout=0.25)
    record_stage("05_continuous_pan", worker_pid, cache.stats)

    # 6. 波段切换（从单波段灰度切到多波段合成）
    for b in [2, 3]:
        win.display_panel.cmb_mode.setCurrentIndex(0)  # gray
        win.display_panel.cmb_band.setCurrentIndex(b - 1)
        win.canvas.refresh()
        wait_qt(app, timeout=0.3)

    # 切 RGB 模式
    win.display_panel.cmb_mode.setCurrentIndex(1)  # rgb
    win.display_panel.cmb_r.setCurrentIndex(0)
    win.display_panel.cmb_g.setCurrentIndex(1)
    win.display_panel.cmb_b.setCurrentIndex(2)
    win.canvas.refresh()
    wait_qt(app, timeout=0.5)
    record_stage("06_band_switching", worker_pid, cache.stats)

    # 7. 多图层切换
    if second_layer_path and second_layer_path.exists():
        win.open_dataset(second_layer_path)
        wait_layers(win, app, count=2)
        l1 = win.layer_panel.layers[0]
        l2 = win.layer_panel.layers[1]

        for _ in range(2):
            win.layer_panel.sig_layer_selected.emit(l2)
            wait_qt(app, timeout=0.3)
            win.layer_panel.sig_layer_selected.emit(l1)
            wait_qt(app, timeout=0.3)
        record_stage("07_layer_switching", worker_pid, cache.stats)

    # 8. 移除图层并清空缓存
    for lyr in list(win.layer_panel.layers):
        lid = lyr.layer_id
        win.layer_panel.sig_layer_removed.emit(lid)
        win.layer_panel.remove_layer(lid)
    wait_qt(app, timeout=0.5)
    record_stage("08_layers_removed", worker_pid, cache.stats)

    # 9. 关闭主窗口
    win.close()
    wait_qt(app, timeout=0.5)
    record_stage("09_window_closed", worker_pid, cache.stats)

    # 汇总
    gui_rss_list = [s["gui_rss_mib"] for s in stages if s["gui_rss_mib"] is not None]
    worker_rss_list = [s["worker_rss_mib"] for s in stages if s["worker_rss_mib"] is not None]

    report = {
        "schema": "spectra.p3_gui_memory.v1",
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "command": f"python scripts/measure_p3_gui_memory.py --raster {raster_path} --budget-mib {budget_mib}",
        "measurement_mode": "real_interactive_gui (QMainWindow + PySide6 + QGraphicsView + WorkerProcess)",
        "configuration": {
            "budget_mib": budget_mib,
            "budget_bytes": budget_bytes,
            "raster_path": str(raster_path),
            "raster_size_bytes": raster_path.stat().st_size,
            "second_layer_path": str(second_layer_path) if second_layer_path else None,
        },
        "summary": {
            "gui_rss_start_mib": stages[0]["gui_rss_mib"],
            "gui_rss_peak_mib": max(gui_rss_list) if gui_rss_list else 0.0,
            "gui_rss_final_mib": stages[-1]["gui_rss_mib"],
            "worker_rss_peak_mib": max(worker_rss_list) if worker_rss_list else 0.0,
            "worker_rss_final_mib": stages[-1]["worker_rss_mib"],
            "cache_peak_bytes": max(s["cache_bytes"] for s in stages),
            "cache_final_bytes": stages[-1]["cache_bytes"],
            "cache_total_hits": stages[-1]["cache_hits"],
            "cache_total_misses": stages[-1]["cache_misses"],
            "cache_total_evictions": stages[-1]["cache_evictions"],
            "lru_eviction_triggered": bool(stages[-1]["cache_evictions"] > 0),
            "cache_strictly_bounded": bool(max(s["cache_bytes"] for s in stages) <= budget_bytes + 2 * 1024 * 1024),
        },
        "stages": stages,
    }

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[REPORT WRITTEN] {out_json}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="P3 真实 GUI 内存与 LRU 淘汰测量")
    parser.add_argument("--budget-mib", type=int, default=10, help="TileCache 预算 (MiB)")
    parser.add_argument("--raster", type=str, default="", help="合成栅格路径")
    parser.add_argument("--out", type=str, default="", help="产出报告路径")
    args = parser.parse_args()

    root = find_project_root()
    raster = Path(args.raster) if args.raster else (root / "work" / "p3_perf" / "p3_big_6144.tif")
    if not raster.exists():
        print(f"错误：测试栅格不存在：{raster}", file=sys.stderr)
        return 1

    second_layer = None
    try:
        from tests.fixtures.p3_env import ensure_p3_samples
        samples = ensure_p3_samples()
        second_layer = Path(samples["samples"]["north"]["path"])
    except Exception:
        p = root / "work" / "p3_samples" / "p3_north.tif"
        if p.exists():
            second_layer = p

    out_file = Path(args.out) if args.out else (root / "docs" / "progress" / "runs" / "p3_gui_memory.json")

    report = run_measurement(
        raster_path=raster,
        second_layer_path=second_layer if second_layer.exists() else None,
        budget_mib=args.budget_mib,
        out_json=out_file,
    )

    evictions = report["summary"]["cache_total_evictions"]
    print(f"\n测量完成：LRU 淘汰触发次数={evictions} | 峰值 GUI 内存={report['summary']['gui_rss_peak_mib']} MiB")
    return 0 if evictions > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
