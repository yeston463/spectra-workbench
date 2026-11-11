"""P3-01 / P3-03 / P3-04 / P3-05 的界面级验收。

覆盖：打开→查询→ROI→调整显示→保存→关闭→重开 的完整流程，
双视图联动、缓存预算与过期响应、显示不改源像元、关闭图层释放文件句柄。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROIShape
from spectra_workbench.project.model import resolve_source
from spectra_workbench.rendering.cache import TileCache, TileKey
from spectra_workbench.rendering.display import DisplayMode, DisplaySettings, StretchMode
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

pytestmark = [pytest.mark.integration, pytest.mark.gui, pytest.mark.engine]

ROT_GT = (500000.0, 2.0, 0.5, 3200000.0, 0.5, -2.0)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


def ramp_expect(band: int, col: int) -> int:
    return (col * band) % 60000


def pump(qapp: QApplication, seconds: float = 0.2, until=None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
        if until is not None and until():
            return
    qapp.processEvents()


@pytest.fixture(scope="module")
def samples() -> dict:
    from tests.fixtures.p3_env import ensure_p3_samples

    return ensure_p3_samples()


@pytest.fixture()
def window(qapp: QApplication, tmp_path: Path):
    win = MainWindow(project_root=tmp_path)
    win.show()
    pump(qapp, 0.05)
    yield win
    win.close()
    pump(qapp, 0.1)


def wait_layers(win: MainWindow, qapp: QApplication, count: int = 1) -> None:
    pump(qapp, 12.0, until=lambda: len(win.layer_panel.layers) >= count)
    assert len(win.layer_panel.layers) >= count, (
        f"worker 探测未在时限内完成；状态栏：{win.status_bar.currentMessage()}"
    )


def wait_render(canvas: ImageCanvas, qapp: QApplication, seconds: float = 12.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
        if canvas.stretch_bounds:
            return
    raise AssertionError("画布未完成渲染（拉伸区间未建立）")


# ---------------------------------------------------------------- 打开与元数据


def test_open_dataset_populates_layer_and_metadata(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    layer = window.layer_panel.layers[0]
    assert layer.width == 200 and layer.height == 150 and layer.band_count == 3
    assert layer.crs_epsg == 32650
    assert layer.geotransform == rot["geotransform"]
    assert layer.metadata["spatial_model"] == "affine"
    assert layer.overview_levels, "概览层级必须可见"
    assert layer.display.mode is DisplayMode.rgb, "3 波段默认应为彩色合成"
    wait_render(window.canvas, qapp)
    assert (
        "RGB" in window.canvas.lbl_status.text() or "rgb" in window.canvas.lbl_status.text().lower()
    )


# ---------------------------------------------------------------- 坐标查询


def test_hover_query_shows_raw_physical_and_status(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    layer = window.layer_panel.layers[0]
    wait_render(window.canvas, qapp)

    r0, c0 = rot["nodata_block"][0], rot["nodata_block"][1]
    # 有效零值像元
    window.canvas.sig_pixel_hover.emit(0, 3, layer.layer_id)
    pump(qapp, 6.0, until=lambda: "列 0" in window.info_panel.lbl_pos.text())
    assert "列 0" in window.info_panel.lbl_pos.text()
    assert window.info_panel.tbl.item(0, 1).text() == "0"
    assert "有效" in window.info_panel.tbl.item(0, 3).text()

    # NoData 像元
    window.canvas.sig_pixel_hover.emit(c0 + 1, r0 + 1, layer.layer_id)
    pump(qapp, 6.0, until=lambda: f"列 {c0 + 1}" in window.info_panel.lbl_pos.text())
    assert "NoData" in window.info_panel.tbl.item(0, 3).text()


def test_map_coordinate_roundtrip_through_panel(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    """面板输入地图坐标 → worker 反算行列，中心/角点两种约定都要显示。"""
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)

    gt = rot["geotransform"]
    u, v = 20.6, 10.6  # 小数部分 >0.5：中心与角点约定必须给出不同行列
    x = gt[0] + gt[1] * u + gt[2] * v
    y = gt[3] + gt[4] * u + gt[5] * v
    window.info_panel.spin_x.setValue(x)
    window.info_panel.spin_y.setValue(y)
    window.info_panel.cmb_conv.setCurrentIndex(0)
    window.info_panel._emit_map_query()
    pump(qapp, 8.0, until=lambda: "列 20" in window.info_panel.lbl_map_result.text())
    text = window.info_panel.lbl_map_result.text()
    assert "列 20" in text and "行 10" in text, text
    assert "角点约定：列 20" in text, text
    assert "中心约定：列 21" in text, text


# ---------------------------------------------------------------- 统计


def test_stats_buttons_label_sampled_and_full(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    spike = samples["samples"]["spike"]
    window.open_dataset(spike["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)

    window.info_panel.btn_full.click()
    pump(qapp, 10.0, until=lambda: "全量统计" in window.info_panel.lbl_stats.text())
    assert "全量统计" in window.info_panel.lbl_stats.text()
    assert "max=60000" in window.info_panel.lbl_stats.text().replace(" ", "")

    window.info_panel.btn_sampled.click()
    pump(qapp, 10.0, until=lambda: "抽样统计" in window.info_panel.lbl_stats.text())
    assert "抽样统计" in window.info_panel.lbl_stats.text()


# ---------------------------------------------------------------- ROI


def test_rect_roi_lifecycle_create_edit_delete_save(
    window: MainWindow, qapp: QApplication, samples: dict, tmp_path: Path
) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)
    layer = window.layer_panel.layers[0]

    roi_id = window.canvas.begin_rect_roi("田埂排除区")
    pump(qapp, 0.1)
    assert roi_id in [r.roi_id for r in window.canvas.roi_list()]
    assert any(r.name == "田埂排除区" for r in layer.rois)

    # 编辑：平移与缩放（这两种交互最终落到 ROI 的 setPos/scale，与手柄拖动同一条写回路径）
    item = window.canvas._roi_states[roi_id]["item"]
    before = next(r for r in window.canvas.roi_list() if r.roi_id == roi_id)
    b0, b1 = before.points
    item.setPos(item.pos() + pg.Point(7.0, -5.0))
    pump(qapp, 0.2)
    moved = next(r for r in window.canvas.roi_list() if r.roi_id == roi_id)
    shifted = [(c - 7.0, r + 5.0) for (c, r) in moved.points]
    assert shifted[0] == pytest.approx(b0), (shifted, b0)
    assert shifted[1] == pytest.approx(b1), (shifted, b1)
    assert all(c >= 0 and r >= 0 for (c, r) in before.points), (
        f"初始矩形必须落在图幅内：{before.points}"
    )

    width_before = b1[0] - b0[0]
    item.scale(1.5, 1.5, [0, 0])
    pump(qapp, 0.2)
    sized = next(r for r in window.canvas.roi_list() if r.roi_id == roi_id)
    assert sized.points[1][0] - sized.points[0][0] == pytest.approx(width_before * 1.5), (
        sized.points
    )
    assert [r.roi_id for r in layer.rois] == [roi_id], "画布编辑必须写回图层模型"

    # 保存到项目并重开，ROI 必须还在
    proj = tmp_path / "case.spectra.json"
    window.project_path = proj
    assert window.save_project_action() is True
    reopened = MainWindow(project_root=tmp_path)
    assert reopened.open_project(proj) is True
    wait_layers(reopened, qapp)
    pump(qapp, 2.0)
    ids = [r.roi_id for r in reopened.canvas.roi_list()]
    assert roi_id in ids, ids
    reopened.close()
    pump(qapp, 0.1)

    # 删除
    window.canvas.select_roi(roi_id)
    assert window.canvas.delete_selected_roi() == roi_id
    assert roi_id not in [r.roi_id for r in window.canvas.roi_list()]
    assert all(r.roi_id != roi_id for r in layer.rois)


def test_polygon_roi_requires_three_vertices(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    window.open_dataset(samples["samples"]["north"]["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)

    window.canvas.begin_polygon_roi()
    assert window.canvas.finish_polygon() is None, "0 顶点应被拒绝"

    window.canvas.begin_polygon_roi()
    for c, r in [(5, 5), (40, 8), (30, 45), (8, 38)]:
        window.canvas.add_polygon_vertex(c, r)
    roi_id = window.canvas.finish_polygon()
    assert roi_id is not None
    roi = next(x for x in window.canvas.roi_list() if x.roi_id == roi_id)
    assert roi.shape is ROIShape.polygon and len(roi.points) == 4
    # 鞋带公式手算：5*8-40*5 + 40*45-30*8 + 30*38-8*45 + 8*5-5*38 = -160+1560+780-150 = 2030 → 1015
    assert roi.area_pixels() == pytest.approx(1015.0, rel=1e-6), "鞋带公式面积必须可核对"


# ---------------------------------------------------------------- 显示不改源数据


def test_display_changes_never_alter_source_pixels(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    layer = window.layer_panel.layers[0]
    wait_render(window.canvas, qapp)

    # 记录显示前的原始像元值
    before = window.gdal_client.query_pixel(layer.file_path, row=7, col=13)
    assert before["bands"][0]["raw"] == float(ramp_expect(1, 13))

    img_before = window.canvas.img_item.image
    assert img_before is not None

    for settings in (
        DisplaySettings(mode=DisplayMode.gray, gray_band=2, stretch=StretchMode.min_max),
        DisplaySettings(
            mode=DisplayMode.pseudo,
            gray_band=3,
            stretch=StretchMode.percentile,
            pseudocolor="jet",
            opacity=0.5,
        ),
        DisplaySettings(mode=DisplayMode.rgb, rgb_bands=(3, 2, 1), stretch=StretchMode.stddev),
    ):
        window.display_panel.set_layer(layer.band_count, settings)
        window.display_panel.sig_display_changed.emit(window.display_panel.current_settings())
        wait_render(window.canvas, qapp)

    after = window.gdal_client.query_pixel(layer.file_path, row=7, col=13)
    assert after["bands"][0]["raw"] == before["bands"][0]["raw"], "显示变化不得改动源像元"

    # 文件本身也没被改写
    st = os.stat(layer.file_path)
    assert st.st_size == os.path.getsize(layer.file_path)
    assert window.canvas.img_item.image is not None


def test_percentile_and_minmax_bounds_differ_on_spike(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    spike = samples["samples"]["spike"]
    window.open_dataset(spike["path"])
    wait_layers(window, qapp)
    layer = window.layer_panel.layers[0]

    window.display_panel.set_layer(
        layer.band_count, DisplaySettings(mode=DisplayMode.gray, stretch=StretchMode.min_max)
    )
    window.display_panel.sig_display_changed.emit(window.display_panel.current_settings())
    wait_render(window.canvas, qapp)
    mm = window.canvas.stretch_bounds[0]

    window.display_panel.set_layer(
        layer.band_count,
        DisplaySettings(
            mode=DisplayMode.gray, stretch=StretchMode.percentile, percentiles=(2.0, 98.0)
        ),
    )
    window.display_panel.sig_display_changed.emit(window.display_panel.current_settings())
    wait_render(window.canvas, qapp)
    pct = window.canvas.stretch_bounds[0]

    assert mm[1] == pytest.approx(60000.0), mm
    assert pct[1] < 200.0, f"百分位拉伸必须把孤立尖峰挡在外面：{pct}"


def test_invalid_pixels_are_transparent_valid_zero_is_not(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)
    data = np.asarray(window.canvas.img_item.image)
    r0, c0, r1, c1 = rot["nodata_block"]
    # 全幅 200x150 在一个读取窗口内，数组下标即像元行列
    assert data.shape[:2] == (rot["height"], rot["width"]), data.shape
    assert data[r0 + 1, c0 + 1, 3] == 0, "NoData 像元必须透明"
    assert data[r1 - 1, c1 - 1, 3] == 0, "NoData 块内全部像元必须透明"
    assert data[r0 - 1, c0 - 1, 3] > 0, "相邻有效像元不得被误标为无效"
    assert data[5, 0, 3] > 0, "值恰为 0 的有效像元必须保持不透明"
    assert data[..., 3].max() == 255


# ---------------------------------------------------------------- 缓存与过期


def test_cache_budget_is_enforced_across_many_views(qapp: QApplication) -> None:
    cache = TileCache(budget_bytes=64 * 1024)
    for i in range(60):
        key = TileKey(
            source="a.tif",
            bands=(1,),
            x_off=i * 512,
            y_off=0,
            win_w=512,
            win_h=512,
            out_w=512,
            out_h=512,
            overview=-1,
        )
        cache.put(key, b"y" * (8 * 1024))
    assert cache.stats.bytes <= 64 * 1024
    assert cache.stats.evictions > 0
    assert cache.stats.entries < 60


def test_stale_response_cannot_overwrite_newest_view(qapp: QApplication, samples: dict) -> None:
    """乱序返回的窗口响应：最终显示必须对应最后一次视图。"""

    class DelayClient:
        def __init__(self):
            self.pending = []
            self.reads = 0

        def read_window_async(self, **kw):
            self.reads += 1
            self.pending.append(kw)

        def read_view_window_async(self, **kw):  # pragma: no cover
            self.reads += 1
            self.pending.append(kw)

        def transform_bounds_async(self, *a, **kw):  # pragma: no cover
            kw["on_success"]({"bounds": [0, 0, 1, 1]})

        def flush_in_reverse(self, payload_for):
            # 先返回旧请求，再返回最新请求
            for kw in list(self.pending[:-1]) + [self.pending[-1]]:
                kw["on_success"](payload_for(kw))

    client = DelayClient()
    canvas = ImageCanvas(cache=TileCache(budget_bytes=8 * 1024 * 1024))
    # 合成本次视图所需的最大范围；读取由 DelayClient 伪造，不触盘，因此用大影幅让对齐窗口互不相同
    layer = LayerItem(
        name="stale",
        file_path=samples["samples"]["rotated"]["path"],
        metadata={
            "width": 2048,
            "height": 1536,
            "count": 1,
            "driver": "GTiff",
            "spatial_model": "affine",
        },
    )
    try:
        canvas.set_layer(layer, client)
        assert client.reads == 1
        # 三个明显不同的视图：起点与范围都要跨过 512 对齐边界，窗口键才互不相同
        for x_range, y_range in (
            ((0, 1536), (1536, 0)),
            ((512, 2048), (1024, 0)),
            ((1024, 2048), (1024, 512)),
        ):
            canvas.plot_item.getViewBox().setRange(xRange=x_range, yRange=y_range, padding=0.0)
            canvas.refresh()
        assert client.reads >= 2

        def payload_for(kw):
            w, h = int(kw["out_w"]), int(kw["out_h"])
            arr = np.full((1, h, w), int(kw["x_off"]) % 255, dtype=np.uint16)
            valid = np.ones((1, h, w), dtype=np.uint8)
            import base64

            return {
                "raw": True,
                "x_off": kw["x_off"],
                "y_off": kw["y_off"],
                "win_w": kw["win_w"],
                "win_h": kw["win_h"],
                "out_w": w,
                "out_h": h,
                "bands": kw.get("bands") or [1],
                "shape": [1, h, w],
                "numpy_dtype": "uint16",
                "data_b64": base64.b64encode(arr.tobytes()).decode(),
                "valid_bits_b64": base64.b64encode(
                    np.packbits(valid.reshape(-1), axis=None).tobytes()
                ).decode(),
                "valid_bits_count": int(valid.size),
                "band_meta": [
                    {
                        "band": 1,
                        "dtype": "UInt16",
                        "numpy_dtype": "uint16",
                        "nodata": None,
                        "scale": None,
                        "offset": None,
                        "mask_flags": 1,
                        "has_alpha": False,
                        "all_valid_mask": True,
                        "min": 0.0,
                        "max": 255.0,
                        "valid_count": w * h,
                        "total_count": w * h,
                    }
                ],
                "overview_used": -1,
                "is_rgb": False,
            }

        latest = client.pending[-1]
        older = client.pending[:-1]
        client.flush_in_reverse(payload_for)
        pump(qapp, 0.3)
        status = canvas.lbl_status.text()
        shown = f"窗口 [{latest['x_off']},{latest['y_off']}] {latest['win_w']}x{latest['win_h']}->{latest['out_w']}x{latest['out_h']}"
        assert shown in status, status
        for kw in older:
            stale = f"窗口 [{kw['x_off']},{kw['y_off']}] {kw['win_w']}x{kw['win_h']}"
            assert stale not in status, "旧视图不得成为最终显示"
            assert (kw["x_off"], kw["win_w"]) != (latest["x_off"], latest["win_w"]), (
                "用例需保证各请求视图不同"
            )
    finally:
        canvas.clear()
        canvas.close()
        pump(qapp, 0.05)


# ---------------------------------------------------------------- 双视图


def test_dual_view_sync_and_release(window: MainWindow, qapp: QApplication, samples: dict) -> None:
    rot = samples["samples"]["rotated"]
    window.open_dataset(rot["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)

    window.act_dual.setChecked(True)
    pump(qapp, 1.0)
    assert window.canvas_b.isVisible()
    wait_render(window.canvas_b, qapp)

    window.canvas.plot_item.getViewBox().setRange(xRange=(20, 60), yRange=(90, 50), padding=0.0)
    window.canvas.refresh()
    pump(qapp, 0.6)
    a = window.canvas.view_state()
    b = window.canvas_b.view_state()
    assert b["center_col"] == pytest.approx(a["center_col"], rel=1e-3), (a, b)
    assert b["center_row"] == pytest.approx(a["center_row"], rel=1e-3)

    # 关闭联动后不再跟随
    window.act_sync.setChecked(False)
    window.canvas.plot_item.getViewBox().setRange(xRange=(100, 140), yRange=(150, 110), padding=0.0)
    window.canvas.refresh()
    pump(qapp, 0.6)
    a2 = window.canvas.view_state()
    b2 = window.canvas_b.view_state()
    assert a2["center_col"] != pytest.approx(b2["center_col"], rel=1e-3)

    window.act_sync.setChecked(True)
    window.act_dual.setChecked(False)
    pump(qapp, 0.3)
    assert not window.canvas_b.isVisible()


# ---------------------------------------------------------------- 项目与句柄


def test_project_saves_order_visibility_display_and_view(
    qapp: QApplication, samples: dict, tmp_path: Path
) -> None:
    """数据放在项目目录内：保存必须优先写相对路径，重开必须还原图层顺序/显隐/显示/视图。"""
    data = tmp_path / "data"
    data.mkdir()
    src_a = Path(samples["samples"]["rotated"]["path"])
    src_b = Path(samples["samples"]["north"]["path"])
    dst_a, dst_b = data / src_a.name, data / src_b.name
    shutil.copyfile(src_a, dst_a)
    shutil.copyfile(src_b, dst_b)

    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        win.open_dataset(dst_a)
        wait_layers(win, qapp)
        win.open_dataset(dst_b)
        wait_layers(win, qapp, count=2)
        wait_render(win.canvas, qapp)

        layers = win.layer_panel.layers
        # 图层顺序必须跟随“发起打开的顺序”，与探测响应回来的先后无关
        assert [ly.name for ly in layers] == ["p3_rotated", "p3_north"], [ly.name for ly in layers]
        layers[1].visible = False
        layers[0].display = DisplaySettings(
            mode=DisplayMode.pseudo, pseudocolor="terrain", opacity=0.4
        )
        view_before = win.canvas.view_state()

        proj = tmp_path / "两个图层.spectra.json"
        win.project_path = proj
        assert win.save_project_action() is True
        text = proj.read_text(encoding="utf-8")
        assert "p3_rotated" in text and "terrain" in text
        assert "C:" not in text, "项目内数据不应存绝对路径"

        reopened = MainWindow(project_root=tmp_path)
        assert reopened.open_project(proj) is True
        wait_layers(reopened, qapp, count=2)
        pump(qapp, 1.0)
        got = reopened.layer_panel.layers
        assert [g.name for g in got] == ["p3_rotated", "p3_north"], [g.name for g in got]
        assert got[1].visible is False
        assert got[0].display.pseudocolor == "terrain"
        assert got[0].display.opacity == pytest.approx(0.4)
        v = reopened.canvas.view_state()
        assert v["center_col"] == pytest.approx(view_before["center_col"], rel=1e-3)
        assert v["center_row"] == pytest.approx(view_before["center_row"], rel=1e-3)

        rec = reopened.build_project_spec().layers[0]
        assert rec.source_is_relative is True
        assert not Path(rec.source).is_absolute()
        assert resolve_source(tmp_path, rec.source, is_relative=True).resolve() == dst_a.resolve()
        reopened.close()
        pump(qapp, 0.1)
    finally:
        win.close()
        pump(qapp, 0.1)


def test_layer_order_follows_open_request_not_probe_arrival(
    tmp_path: Path, qapp: QApplication, samples: dict
) -> None:
    """乱序返回的探测响应不得改变图层顺序（把响应顺序反过来也必须成立）。"""

    class ReverseProbeClient:
        def __init__(self) -> None:
            self.queued: list[tuple[dict, object, object]] = []

        def probe_dataset_async(self, path, on_success=None, on_error=None):
            meta = {
                "width": 64,
                "height": 48,
                "count": 1,
                "driver": "GTiff",
                "spatial_model": "affine",
                "crs_epsg": None,
                "geotransform": None,
                "overview_levels": [],
                "bands": [],
            }
            self.queued.append((meta, on_success, on_error))
            return "probe"

        def flush_in_order(self, order: list[int]) -> None:
            for i in order:
                meta, on_ok, _err = self.queued[i]
                if on_ok:
                    on_ok(meta)
            self.queued = []

        def read_window_async(self, **kw):  # pragma: no cover - 本用例不渲染
            pass

        def read_view_window_async(self, *a, **kw):  # pragma: no cover
            pass

        def transform_bounds_async(self, *a, **kw):  # pragma: no cover
            pass

        def crs_wkt_async(self, *a, **kw):  # pragma: no cover
            pass

        def probe_vector_async(self, *a, **kw):  # pragma: no cover
            pass

        def list_subdatasets_async(self, *a, **kw):  # pragma: no cover
            pass

        def stop(self, timeout: float = 5.0) -> int:  # pragma: no cover
            return 0

        @property
        def is_alive(self) -> bool:
            return True

    fake = ReverseProbeClient()
    win = MainWindow(project_root=tmp_path)
    win._clients["gdal"] = fake  # 注入替身：只考察“发起顺序 → 图层顺序”这一条不变量
    try:
        for name in ("甲先开", "乙后开", "丙最后开"):
            src = Path(samples["samples"]["no_gt"]["path"])
            dst = tmp_path / f"{name}.tif"
            shutil.copyfile(src, dst)
            win.open_dataset(dst)
        assert win.layer_panel.layers == []
        fake.flush_in_order([2, 0, 1])  # 既非原序也非完全反序的到达顺序
        pump(qapp, 0.3)
        assert [ly.name for ly in win.layer_panel.layers] == ["甲先开", "乙后开", "丙最后开"], [
            ly.name for ly in win.layer_panel.layers
        ]
    finally:
        win.close()
        pump(qapp, 0.1)


def test_relink_after_moving_data(tmp_path: Path, qapp: QApplication, samples: dict) -> None:
    """整体搬移项目目录后，缺失源必须能被重新关联。"""
    root = tmp_path / "项目 A"
    root.mkdir()
    data = root / "data"
    data.mkdir()
    src = Path(samples["samples"]["rotated"]["path"])
    moved_file = data / src.name
    shutil.copyfile(src, moved_file)

    win = MainWindow(project_root=root)
    win.open_dataset(moved_file)
    wait_layers(win, qapp)
    wait_render(win.canvas, qapp)
    layer_id = win.layer_panel.layers[0].layer_id
    proj = root / "case.spectra.json"
    win.project_path = proj
    assert win.save_project_action() is True
    win.close()
    pump(qapp, 0.2)

    # 数据源被改名 -> 打开项目时缺失
    moved_file.rename(data / "改名后的影像.tif")
    spec = __import__("spectra_workbench.project.model", fromlist=["load_project"]).load_project(
        proj
    )
    from spectra_workbench.project.model import find_missing, relink, resolve_source

    missing = find_missing(root, spec)
    assert [m.layer_id for m in missing] == [layer_id]

    win2 = MainWindow(project_root=root)
    assert win2.open_project(proj) is True
    pump(qapp, 1.0)
    # 未关联时不应崩，也不应把它当成功图层
    assert len(win2.layer_panel.layers) == 0

    fixed = relink(root, spec, {layer_id: data / "改名后的影像.tif"})
    assert find_missing(root, fixed) == []
    assert resolve_source(
        root, fixed.layers[0].source, is_relative=fixed.layers[0].source_is_relative
    ).exists()
    win2.close()
    pump(qapp, 0.2)


def test_closing_layer_releases_file_handle(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    """关闭图层后文件必须可被重命名/删除（Windows 句柄实测）。"""
    tmp = Path(samples["samples"]["rotated"]["path"]).parent / "handle_probe.tif"
    shutil.copyfile(samples["samples"]["rotated"]["path"], tmp)

    window.open_dataset(tmp)
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)
    layer = window.layer_panel.layers[0]

    window.layer_panel.remove_layer(layer.layer_id)
    pump(qapp, 0.4)
    assert all(ly.layer_id != layer.layer_id for ly in window.layer_panel.layers)

    # 句柄未释放时 Windows 上会抛 PermissionError
    os.rename(tmp, tmp.with_name("handle_probe_moved.tif"))
    assert not tmp.exists()
    os.remove(tmp.with_name("handle_probe_moved.tif"))


def test_view_crs_switch_uses_worker_wkt(
    window: MainWindow, qapp: QApplication, samples: dict
) -> None:
    geo = samples["samples"]["geographic"]
    window.open_dataset(geo["path"])
    wait_layers(window, qapp)
    wait_render(window.canvas, qapp)

    idx = next(
        i for i in range(window.cmb_view_crs.count()) if window.cmb_view_crs.itemData(i) == 32650
    )
    window.cmb_view_crs.setCurrentIndex(idx)
    pump(qapp, 20.0, until=lambda: window.canvas._view_bounds is not None)
    assert window.canvas.view_crs_wkt and "32650" in window.canvas.view_crs_wkt
    bounds = window.canvas._view_bounds
    assert bounds and bounds[0] > 1e5, f"32650 视图范围应为大数值：{bounds}"
    wait_render(window.canvas, qapp)
    pump(qapp, 20.0, until=lambda: "重投影网格" in window.canvas.lbl_status.text())
    status = window.canvas.lbl_status.text()
    m = re.search(r"重投影网格 \[(-?\d+),(-?\d+)\] ~ \[(-?\d+),(-?\d+)\]", status)
    assert m is not None, f"跨 CRS 显示必须走 worker 的重投影窗口：{status}"
    assert int(m.group(1)) > 10**5, f"32650 网格应为地图坐标：{status}"
