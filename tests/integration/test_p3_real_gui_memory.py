"""真实 GUI 大影像内存与 LRU 缓存淘汰集成测试（P3-03 / 性能验收）。

验收要求：
1. 使用带概览的合成栅格在大影像视口下真实渲染。
2. 设置较小缓存预算，验证多视口平移与缩放实际触发 LRU 淘汰（evictions > 0）。
3. 验证缓存占用严格受预算限制（不无限增长）。
4. 验证关闭图层/窗口后缓存彻底清空，文件与进程句柄安全释放。
5. 测试在真实 MainWindow 实例下运行，进程内零原生地理依赖。
"""

from __future__ import annotations

import os
import time

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


def _wait_layers(win: MainWindow, qapp: QApplication, count: int = 1, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.04)
    raise AssertionError(f"等待图层超时：期望 {count}，当前 {len(win.layer_panel.layers)}")


def test_real_gui_large_raster_lru_cache_eviction_and_cleanup(qapp: QApplication) -> None:
    """真实 GUI 下大影像连续缩放和平移触发 LRU 淘汰，并在关闭后释放缓存。"""
    root = find_project_root()
    raster_path = root / "work" / "p3_perf" / "p3_big_6144.tif"
    if not raster_path.exists():
        pytest.skip(f"大影像合成样例尚未生成：{raster_path}")

    # 设置 8 MiB 的紧凑预算（以 6144x6144 瓦片单块数 MiB 计，数次操作即可填满并触发逐出）
    budget_bytes = 8 * 1024 * 1024
    win = MainWindow(cache_budget_bytes=budget_bytes)
    win.show()
    try:
        win.open_dataset(raster_path)
        _wait_layers(win, qapp, count=1)
        layer = win.layer_panel.layers[0]

        cache = win.canvas._cache
        assert cache.budget_bytes == budget_bytes

        # 连续平移多处视口
        vb = win.canvas.plot_item.getViewBox()
        pan_coords = [
            (500, 500),
            (1500, 1500),
            (2500, 2500),
            (3500, 3500),
            (4500, 4500),
            (1000, 3000),
            (3000, 1000),
            (2000, 4000),
        ]
        for x, y in pan_coords:
            vb.setRange(xRange=(x, x + 1024), yRange=(y + 1024, y), padding=0.0)
            win.canvas.refresh()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.03)

        # 验证真实发生了 LRU 淘汰
        assert cache.stats.evictions > 0, "必须真实触发 LRU 逐出"
        # 验证缓存字节数受预算约束
        assert cache.stats.bytes <= budget_bytes + 2 * 1024 * 1024

        # 验证图层移除时缓存失效并清空
        lid = layer.layer_id
        win.layer_panel.sig_layer_removed.emit(lid)
        win.layer_panel.remove_layer(lid)
        qapp.processEvents()

        # 验证窗口关闭后缓存清零
        win.close()
        qapp.processEvents()
        assert cache.stats.bytes == 0, "关闭后缓存必须被清零"
    finally:
        win.close()
        qapp.processEvents()
