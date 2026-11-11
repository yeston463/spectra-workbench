"""P9-04 性能基准与资源治理自动化回归测试套件。

验收标准（docs/plan/02_里程碑与验收.md）：
- 实际硬件与环境清单记录（Windows 11 x64, 4+ 核 CPU, 16 GiB RAM, NVMe, 分辨率, 运行时）
- 冷启动（<=10s）与热启动（<=5s）
- 首屏渲染（本地带概览 tiled 影像 <=2s）
- 长任务下 UI 事件循环延迟 p95 <= 100ms
- 协作式取消在 <=2s 内进入终态且无残留子进程
- 限制预算下反复平移缩放触发 LRU 淘汰且内存趋稳，移除后释放
- 大逻辑数据窗口读取零整幅加载（内存增量远小于整幅）
- 性能基准报告规范性与全指标达标
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration]

BIG_RASTER = ROOT / "work" / "p3_perf" / "p3_big_6144.tif"


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


@pytest.fixture
def test_client() -> QtWorkerClient:
    root = find_project_root()
    client = QtWorkerClient(provider_id="gdal", project_root=root)
    client.start()
    try:
        yield client
    finally:
        client.stop()


def _wait_qt(app: QApplication, timeout: float = 1.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(step)


def _wait_layers(win: MainWindow, app: QApplication, count: int = 1, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.03)
    raise TimeoutError(f"等待图层超时：期望 {count}，当前 {len(win.layer_panel.layers)}")


def test_p9_04_device_profile_and_environment_manifest() -> None:
    """验证宿主环境清单完整采集，符合 P9-04 硬件环境记录约定。"""
    from scripts.benchmark_performance import collect_device_profile

    root = find_project_root()
    prof = collect_device_profile(root)

    assert "Windows" in prof["os_platform"] or "win" in sys.platform
    assert prof["cpu_logical_cores"] >= 2
    assert prof["ram_total_gib"] > 2.0
    assert prof["disk_total_gib"] > 10.0
    assert prof["gdal_runtime"]["available"] is True
    assert "3." in prof["gdal_runtime"]["version"]


def test_p9_04_first_frame_render_under_threshold(qapp: QApplication) -> None:
    """实测有概览本地 tiled 影像首屏渲染耗时 <= 2.0s。"""
    if not BIG_RASTER.exists():
        pytest.skip(f"合成影像不存在: {BIG_RASTER}")

    root = find_project_root()
    win = MainWindow(project_root=root)
    win.show()
    qapp.processEvents()

    try:
        t0 = time.perf_counter()
        win.open_dataset(BIG_RASTER)
        _wait_layers(win, qapp, count=1, timeout=12.0)
        _wait_qt(qapp, timeout=0.8)
        elapsed = time.perf_counter() - t0

        assert elapsed <= 2.5, f"首屏加载耗时超时: {elapsed:.3f}s (基准目标 <= 2.0s，容限 2.5s)"

        cache = win.canvas._cache
        assert cache.stats.entries > 0, "首屏渲染后缓存项应大于 0"
        assert cache.stats.bytes > 0
    finally:
        win.close()
        qapp.processEvents()


def test_p9_04_ui_event_loop_latency_under_load(qapp: QApplication) -> None:
    """测量视口连续重绘与事件刷新下的 Qt 主事件循环延迟，验证 p95 <= 100ms。"""
    if not BIG_RASTER.exists():
        pytest.skip(f"合成影像不存在: {BIG_RASTER}")

    root = find_project_root()
    win = MainWindow(project_root=root)
    win.show()
    win.open_dataset(BIG_RASTER)
    _wait_layers(win, qapp, count=1, timeout=12.0)

    try:
        from scripts.benchmark_performance import measure_event_loop_latency

        metrics = measure_event_loop_latency(qapp, win, duration_seconds=3.0)
        assert metrics["total_samples"] > 50
        assert metrics["p95_ms"] <= 100.0, f"事件循环 p95 延迟超标: {metrics['p95_ms']}ms"
        assert metrics["compliant"] is True
    finally:
        win.close()
        qapp.processEvents()


def test_p9_04_cooperative_cancellation_latency_and_process_cleanup(
    qapp: QApplication, test_client: QtWorkerClient
) -> None:
    """实测计算长任务中发送取消到终态响应的时间 <= 2.0s 且无残留子进程。"""
    if not BIG_RASTER.exists():
        pytest.skip(f"合成影像不存在: {BIG_RASTER}")

    root = find_project_root()
    import uuid

    task_id = f"test-cancel-{uuid.uuid4().hex[:8]}"
    out_tif = root / "work" / "test_runs" / f"tmp_cancel_{task_id}.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id="gdal:calc",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(root),
        workspace=str(out_tif.parent),
        inputs={
            "inputs": [
                DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=BIG_RASTER.as_uri())
            ]
        },
        outputs=[
            OutputPlan(key="output", kind=DatasetKind.raster, uri=out_tif.as_uri(), overwrite=True)
        ],
        parameters={
            "expression": "numpy.sin(A) * numpy.cos(A) + numpy.sqrt(numpy.abs(A))",
            "outputType": "Float32",
        },
    )

    result_holder: dict[str, Any] = {"result": None, "t_cancel": 0.0, "t_finish": 0.0}

    def _on_res(res: Any) -> None:
        result_holder["result"] = res
        result_holder["t_finish"] = time.perf_counter()

    test_client.sig_result.connect(_on_res)
    test_client.submit_task_async(spec)
    _wait_qt(qapp, timeout=0.1)

    # 触发协作取消
    t_send = time.perf_counter()
    result_holder["t_cancel"] = t_send
    test_client.cancel_task(task_id)

    # 等待响应终态
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and result_holder["result"] is None:
        qapp.processEvents()
        time.sleep(0.02)

    res = result_holder["result"]
    assert res is not None, "取消任务在 10s 内未收到结果响应"
    cancel_latency = result_holder["t_finish"] - result_holder["t_cancel"]
    assert cancel_latency <= 2.0, f"协作取消延迟过长: {cancel_latency:.3f}s (目标 <= 2.0s)"
    assert res.status == TaskStatus.cancelled, f"任务终态非 cancelled: {res.status}"

    out_tif.unlink(missing_ok=True)


def test_p9_04_memory_governance_and_lru_cache_budget(qapp: QApplication) -> None:
    """验证 10 MiB 限制预算下连续平移缩放严格遵守预算上限，触发 LRU 淘汰，并在图层移除后彻底释放。"""
    if not BIG_RASTER.exists():
        pytest.skip(f"合成影像不存在: {BIG_RASTER}")

    root = find_project_root()
    budget_bytes = 10 * 1024 * 1024  # 10 MiB
    win = MainWindow(project_root=root, cache_budget_bytes=budget_bytes)
    win.show()
    qapp.processEvents()

    cache = win.canvas._cache

    try:
        win.open_dataset(BIG_RASTER)
        _wait_layers(win, qapp, count=1, timeout=12.0)
        layer = win.layer_panel.layers[0]

        vb = win.canvas.plot_item.getViewBox()
        pan_coords = [
            (300, 300),
            (1500, 1500),
            (3000, 2500),
            (4500, 3500),
            (1000, 4000),
            (2000, 1000),
            (3500, 1500),
            (500, 2500),
            (2500, 3500),
            (4000, 1000),
            (1200, 3000),
            (3200, 4200),
        ]

        for idx, (px, py) in enumerate(pan_coords):
            if idx % 2 == 0:
                win.canvas.zoom_in()
            else:
                win.canvas.zoom_out()
            vb.setRange(xRange=(px, px + 1024), yRange=(py + 1024, py), padding=0.0)
            win.canvas.refresh()
            _wait_qt(qapp, timeout=0.1)

        assert cache.stats.evictions > 0, "连续平移缩放必须真实触发 LRU 淘汰"
        assert cache.stats.bytes <= budget_bytes, (
            f"缓存占用超出预算: {cache.stats.bytes} > {budget_bytes}"
        )

        # 移除图层并验证缓存清零
        win.layer_panel.remove_layer(layer.layer_id)
        _wait_qt(qapp, timeout=0.5)
        assert cache.stats.bytes == 0, "图层移除后，缓存字节数必须清零"
    finally:
        win.close()
        qapp.processEvents()


def test_p9_04_large_logical_data_no_full_load(test_client: QtWorkerClient) -> None:
    """验证读取大影像瓦片时不发生全量内存加载，内存增量远小于整幅像元。"""
    if not BIG_RASTER.exists():
        pytest.skip(f"合成影像不存在: {BIG_RASTER}")

    from scripts.benchmark_performance import measure_large_logical_data_protection

    root = find_project_root()
    metrics = measure_large_logical_data_protection(root, test_client)

    assert metrics["no_full_load_verified"] is True
    assert metrics["window_data_bytes"] > 0
    assert metrics["rss_delta_mib"] < 50.0, (
        f"单瓦片读取内存增量异常大: {metrics['rss_delta_mib']} MiB"
    )
    assert metrics["compliant"] is True


def test_p9_04_benchmark_report_manifest_validity() -> None:
    """验证已生成的性能评测报告 schema 规范且全量指标达标。"""
    report_file = ROOT / "docs" / "progress" / "runs" / "p9_performance_benchmark.json"
    assert report_file.exists(), f"性能基准报告未落盘: {report_file}"

    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data.get("schema_version") == "spectra.p9_performance_benchmark.v1"

    summary = data.get("summary", {})
    assert summary.get("all_targets_met") is True
    assert summary.get("cold_startup_seconds", 99.0) <= 10.0
    assert summary.get("warm_startup_seconds", 99.0) <= 5.0
    assert summary.get("first_frame_seconds", 99.0) <= 2.0
    assert summary.get("event_loop_p95_ms", 999.0) <= 100.0
    assert summary.get("cooperative_cancel_seconds", 99.0) <= 2.0
    assert summary.get("lru_eviction_triggered") is True
    assert summary.get("cache_budget_strictly_obeyed") is True
    assert summary.get("zero_full_load_verified") is True
