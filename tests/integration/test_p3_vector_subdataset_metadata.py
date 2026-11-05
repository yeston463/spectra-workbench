"""P3-02 矢量元数据与子数据集的集成测试（GUI + 真实 worker + 合成容器）。

验收要点：
1. 矢量元数据：图层名称、几何类型、CRS、范围、字段名称/类型/可空信息；
   含空图层与无 CRS 图层的明确标注；要素数未知时如实标记。
2. 子数据集：发现列表保留引擎返回的 URI（**不是普通路径**）；
   选中后由真实 worker 探测，验证尺寸、dtype、NoData、波段与空间信息。
3. 中文/空格/引号路径与缺失子数据集有明确错误。
4. GUI 零原生地理库导入：断言 UI 模块不导入 osgeo / rasterio / gdal。
5. 坏文件报错后 worker 仍能继续处理正常请求。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROI, ROIShape
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


@pytest.fixture(scope="module")
def p3_samples() -> dict:
    from tests.fixtures.p3_env import ensure_p3_samples

    return ensure_p3_samples()


def _wait_until(qapp: QApplication, predicate, timeout: float = 15.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时：{what}")


def test_gui_does_not_import_native_geospatial(qapp: QApplication) -> None:
    """GUI 进程不得导入任何原生地理依赖（AGENTS.md 硬约束）。"""
    for mod in ("osgeo", "rasterio", "gdal"):
        assert mod not in os.sys.modules, f"GUI 进程不应导入 {mod}"


def test_vector_metadata_through_real_worker(qapp: QApplication, p3_samples: dict) -> None:
    """矢量元数据经真实 worker 探测，GUI 展示结构信息。"""
    gpkg = p3_samples["samples"]["vector_gpkg"]
    path = Path(gpkg["path"]).resolve()

    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))
        info = client.probe_vector(str(path))

        assert info["kind"] == "vector"
        assert info["layer_count"] == len(gpkg["layer_names"])
        # GPKG 等驱动不保证图层顺序，按名称集合比较
        names = [lyr["name"] for lyr in info["layers"]]
        assert set(names) == set(gpkg["layer_names"]), f"图层名称不符：{names}"

        point = next(lyr for lyr in info["layers"] if lyr["name"] == gpkg["point_layer"])
        assert point["geometry_type"] == "Point"
        assert point["crs_epsg"] == 4326
        assert point["feature_count"] == gpkg["feature_count"]
        assert point["extent_available"] is True
        assert len(point["extent"]) == 4
        minx, maxx, miny, maxy = point["extent"]
        assert minx < maxx and miny < maxy, f"范围不合理：{point['extent']}"

        field_names = [f["name"] for f in point["fields"]]
        assert field_names == gpkg["field_names"], f"字段名不符：{field_names}"
        for f in point["fields"]:
            assert f["type"], "字段类型不得为空"
            assert isinstance(f["nullable"], bool), "字段可空性必须是明确的布尔值"
            assert "type_code" in f

        # 空图层：要素数为 0，字段结构仍在
        empty = next(lyr for lyr in info["layers"] if lyr["name"] == gpkg["empty_layer"])
        assert empty["feature_count"] == 0
        assert empty["field_count"] >= 1

        # 无 CRS 图层：必须如实标注为无 CRS，不得编造 EPSG
        nocrs = next(lyr for lyr in info["layers"] if lyr["name"] == gpkg["no_crs_layer"])
        assert nocrs["crs_epsg"] is None
        assert nocrs["crs_wkt"] is None
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_vector_metadata_in_gui_panel(qapp: QApplication, p3_samples: dict) -> None:
    """GUI 元数据面板展示矢量结构，且不提供矢量渲染假入口。"""
    gpkg = p3_samples["samples"]["vector_gpkg"]
    path = Path(gpkg["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(path)
        _wait_until(
            qapp,
            lambda: bool(win.layer_panel.layers),
            timeout=20.0,
            what="图层加载",
        )
        _wait_until(
            qapp,
            lambda: win.metadata_panel.lst_vector_layers.count() > 0,
            timeout=20.0,
            what="矢量元数据探测完成",
        )

        panel = win.metadata_panel
        assert panel.lst_vector_layers.count() == len(gpkg["layer_names"])
        assert {
            panel.lst_vector_layers.item(i).text()
            for i in range(panel.lst_vector_layers.count())
        } == set(gpkg["layer_names"])
        summary = panel.lbl_vector_summary.text()
        assert "图层数" in summary, f"摘要应显示图层数：{summary}"

        # 选中点图层 -> 字段表与详情
        for i in range(panel.lst_vector_layers.count()):
            if panel.lst_vector_layers.item(i).text() == gpkg["point_layer"]:
                panel.lst_vector_layers.setCurrentRow(i)
                break
        qapp.processEvents()
        assert panel.tbl_fields.rowCount() == len(gpkg["field_names"])
        assert panel.tbl_fields.item(0, 0).text() == gpkg["field_names"][0]
        detail = panel.lbl_vector_detail.text()
        assert "Point" in detail, f"详情应含几何类型：{detail}"
        assert "EPSG:4326" in detail, f"详情应含 CRS：{detail}"

        # 不提供矢量渲染假入口
        panel_text = panel.lbl_vector_summary.text() + panel.lbl_vector_detail.text()
        assert "渲染" not in panel_text.replace("暂不支持矢量渲染", ""), "不应出现可点击的渲染承诺"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_vector_metadata_chinese_path(qapp: QApplication, p3_samples: dict) -> None:
    """中文与空格路径下的矢量元数据可正确读取。"""
    gj = p3_samples["samples"]["vector_geojson"]
    path = Path(gj["path"]).resolve()
    assert " " in str(path), "样例应包含空格路径"

    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))
        info = client.probe_vector(str(path))
        assert [lyr["name"] for lyr in info["layers"]] == gj["layer_names"]
        fields = info["layers"][0]["fields"]
        assert [f["name"] for f in fields] == gj["field_names"]
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_missing_vector_reports_clear_error(qapp: QApplication, p3_samples: dict) -> None:
    """缺失矢量文件必须给出明确错误，而不是静默返回空结果。"""
    gpkg = p3_samples["samples"]["vector_gpkg"]
    missing = Path(gpkg["path"]).parent / "不存在的文件.gpkg"

    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))
        with pytest.raises(RuntimeError) as exc:
            client.probe_vector(str(missing))
        assert "不存在" in str(exc.value) or "not found" in str(exc.value).lower()
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_subdataset_discovery_and_probe(qapp: QApplication, p3_samples: dict) -> None:
    """子数据集发现与选中探测：URI 原样保留，探测结果完整。"""
    container = p3_samples["samples"]["subdataset_container"]
    path = Path(container["path"]).resolve()

    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))
        listing = client.list_subdatasets(str(path))
        assert listing["subdataset_count"] == container["subdataset_count"]
        assert listing["subdataset_count"] >= 1

        for entry in listing["subdatasets"]:
            uri = entry["uri"]
            # 引擎返回的是数据源引用（协议 URI），不是普通文件路径
            assert uri.startswith(container["uri_prefix"]), f"URI 前缀不符：{uri}"
            assert entry["description"], "子数据集必须有可读描述"

        uri = listing["subdatasets"][0]["uri"]
        detail = client.probe_subdataset(uri)
        assert detail["kind"] == "subdataset"
        assert detail["uri"] == uri, "探测结果必须保留原始 URI"
        assert detail["width"] > 0 and detail["height"] > 0
        assert detail["count"] >= 1
        assert detail["dtypes"], "必须报告 dtype"
        assert detail["nodata"] == -9999.0, f"NoData 不符：{detail['nodata']}"
        assert detail["crs_epsg"] == 4326
        assert isinstance(detail["geotransform"], list) and len(detail["geotransform"]) == 6
        assert detail["bands"], "必须报告波段信息"
        assert detail["bands"][0]["dtype"] == "Float32"
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_missing_subdataset_reports_clear_error(qapp: QApplication, p3_samples: dict) -> None:
    """缺失子数据集必须给出明确错误。"""
    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))
        with pytest.raises(RuntimeError) as exc:
            client.probe_subdataset('NETCDF:"不存在的文件.nc":NoSuchVariable')
        assert "not" in str(exc.value).lower() or "找不到" in str(exc.value)
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_worker_survives_bad_file_and_keeps_serving(
    qapp: QApplication, p3_samples: dict
) -> None:
    """坏文件报错后，worker 仍能继续处理正常请求（不得因一次失败而整体不可用）。"""
    gpkg = p3_samples["samples"]["vector_gpkg"]
    container = p3_samples["samples"]["subdataset_container"]

    client = None
    try:
        from spectra_workbench.scheduler.client import QtWorkerClient

        client = QtWorkerClient(provider_id="gdal", project_root=str(Path.cwd()))

        # 连续制造坏输入
        for bad in (
            str(Path(gpkg["path"]).parent / "missing.gpkg"),
            "NETCDF:\"missing.nc\":Var",
        ):
            with pytest.raises(RuntimeError):
                if bad.endswith(".gpkg"):
                    client.probe_vector(bad)
                else:
                    client.probe_subdataset(bad)

        # 正常请求仍可用
        assert client.probe_vector(gpkg["path"])["layer_count"] >= 1
        assert client.list_subdatasets(container["path"])["subdataset_count"] >= 1
    finally:
        if client is not None:
            client.stop(timeout=3.0)


def test_subdataset_panel_in_gui(qapp: QApplication, p3_samples: dict) -> None:
    """GUI 中选中子数据集后展示探测详情。"""
    container = p3_samples["samples"]["subdataset_container"]
    path = Path(container["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(path)
        _wait_until(
            qapp,
            lambda: bool(win.layer_panel.layers),
            timeout=20.0,
            what="图层加载",
        )
        _wait_until(
            qapp,
            lambda: win.metadata_panel.cmb_subdataset.count() > 0,
            timeout=20.0,
            what="子数据集枚举完成",
        )

        panel = win.metadata_panel
        assert panel.cmb_subdataset.count() == container["subdataset_count"]
        assert "子数据集数" in panel.lbl_subdataset_summary.text()

        # 选中第一项 -> 触发探测 -> 详情可读
        panel.cmb_subdataset.setCurrentIndex(0)
        _wait_until(
            qapp,
            lambda: "尺寸" in panel.lbl_subdataset_detail.text(),
            timeout=20.0,
            what="子数据集探测完成",
        )
        detail = panel.lbl_subdataset_detail.text()
        assert "EPSG:4326" in detail, f"详情应含 CRS：{detail}"
        assert "Float32" in detail, f"详情应含 dtype：{detail}"
        assert "NoData=-9999" in detail, f"详情应含 NoData：{detail}"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_raster_open_still_works_with_metadata_panel(qapp: QApplication, p3_samples: dict) -> None:
    """新增元数据面板不得影响既有栅格打开与 ROI 流程。"""
    rotated = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated)
        _wait_until(qapp, lambda: bool(win.layer_panel.layers), timeout=20.0, what="图层加载")

        roi = ROI(
            name="R",
            shape=ROIShape.rectangle,
            points=[(10.0, 10.0), (20.0, 20.0)],
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()
        assert len(win.canvas.roi_list()) == 1
        assert win.canvas.roi_list()[0].points == [(10.0, 10.0), (20.0, 20.0)]
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
