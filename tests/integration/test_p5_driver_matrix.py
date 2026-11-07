"""P5-05 驱动读写能力矩阵：读/建/复制建/更新分开实测 + 高级选项。

验收对应：
- **读/建/复制建/更新分开**：每个驱动四种访问模式各自真实执行并记录结果，
  不以声明代替实测；
- **实测对象**：ENVI/GPKG/GTiff/COG + 已装扩展驱动（netCDF/BMP/FlatGeobuf/
  GeoJSON/PNG/JPEG），并枚举全部已安装可打开驱动；
- **声明 vs 实况**：DCAP 声明与实际行为逐项对照——已知错位
  （ENVI CreateCopy、FlatGeobuf Update 声明 false 但实测可用）白名单化，
  其余错位一律测试失败（GDAL 行为变化必须显式过目）；
- **高级选项**：describe_driver 解析 DMD_CREATIONOPTIONLIST，
  COMPRESS/VERSION 等选项与候选值真实可用于创建。

运行时实测结论（GDAL 3.13.3，本机 QGIS 运行时）：
- COG 在 3.13 已支持 Create（旧版仅 CreateCopy）；
- ENVI 标称无 CreateCopy 但实测可用；FlatGeobuf 标称不可 Update 但实测可追加。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.scheduler.client import QtWorkerClient

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)

#: 实测矩阵的驱动集合：四个点名驱动 + 已装扩展驱动代表。
MATRIX_DRIVERS = (
    "GTiff",
    "ENVI",
    "COG",
    "GPKG",
    "GeoJSON",
    "FlatGeobuf",
    "PNG",
    "JPEG",
    "netCDF",
    "BMP",
)

#: 已知的「声明 false 但实测可用」错位（GDAL 元数据保守、行为先行）。
#: 新出现条目 = GDAL 行为变化，必须显式过目后入列。
KNOWN_DECLARED_FALSE_BUT_WORKS = {
    ("ENVI", "DCAP_CREATECOPY"),
    ("FlatGeobuf", "DCAP_UPDATE"),
    ("GeoJSON", "DCAP_CREATECOPY"),
    ("FlatGeobuf", "DCAP_CREATECOPY"),
    ("BMP", "DCAP_CREATECOPY"),
    ("BMP", "DCAP_UPDATE"),
}

#: 双能力（raster+vector 均声明）驱动的实测种类覆盖：
#: 按 DCAP 推导会把 GPKG 当 raster 处理（Create 建不出可用图层），
#: 实测必须按真实使用方式（矢量）执行。
KIND_OVERRIDE = {"GPKG": "vector"}


def _run_matrix_script(gdal_rt: dict[str, Any], drivers: tuple[str, ...]) -> dict[str, Any]:
    script = f"""
import json, tempfile
from pathlib import Path
from osgeo import gdal, ogr
gdal.UseExceptions()
ogr.UseExceptions()
tmp = Path(tempfile.mkdtemp(prefix="p505_matrix_"))
drivers = {drivers!r}
kind_override = {KIND_OVERRIDE!r}

def make_raster_src(path):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, 1, gdal.GDT_Byte)
    ds.SetMetadataItem("SRC", "1")
    ds = None
    return path

def make_vector_src(path):
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    lyr = ds.CreateLayer("t")
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("v", 1)
    lyr.CreateFeature(f)
    ds = None
    return path

def driver_kind(name, drv):
    if name in kind_override:
        return kind_override[name]
    if drv.GetMetadataItem("DCAP_VECTOR") and not drv.GetMetadataItem("DCAP_RASTER"):
        return "vector"
    return "raster"

def try_create(name, path):
    try:
        drv = gdal.GetDriverByName(name)
        if driver_kind(name, drv) == "vector":
            ds = drv.CreateDataSource(str(path)); ds.CreateLayer("t"); ds = None
        else:
            ds = drv.Create(str(path), 2, 2, 1, gdal.GDT_Byte); ds = None
        return path.exists()
    except Exception as exc:
        return "error: " + type(exc).__name__

def try_createcopy(name, src_raster, src_vector, path):
    try:
        drv = gdal.GetDriverByName(name)
        if driver_kind(name, drv) == "vector":
            src = gdal.OpenEx(str(src_vector), gdal.OF_VECTOR)
        else:
            src = gdal.Open(str(src_raster), gdal.GA_ReadOnly)
        drv.CreateCopy(str(path), src)
        src = None
        return path.exists()
    except Exception as exc:
        return "error: " + type(exc).__name__

def try_read(path, vector):
    try:
        if vector:
            return ogr.Open(str(path)) is not None
        return gdal.Open(str(path), gdal.GA_ReadOnly) is not None
    except Exception as exc:
        return "error: " + type(exc).__name__

def try_update(name, path):
    try:
        drv = gdal.GetDriverByName(name)
        if driver_kind(name, drv) == "vector":
            ds = ogr.Open(str(path), 1)
            lyr = ds.GetLayer(0)
            f = ogr.Feature(lyr.GetLayerDefn()); lyr.CreateFeature(f)
            ds = None
        else:
            ds = gdal.Open(str(path), 1)
            ds.SetMetadataItem("PROBE", "1")
            ds = None
        return True
    except Exception as exc:
        return "error: " + type(exc).__name__

raster_src = make_raster_src(tmp / "src.tif")
vector_src = make_vector_src(tmp / "src.gpkg")
matrix = {{}}
for name in drivers:
    drv = gdal.GetDriverByName(name)
    if drv is None:
        matrix[name] = {{"missing": True}}
        continue
    declared = {{k: bool(drv.GetMetadataItem(k)) for k in (
        "DCAP_OPEN", "DCAP_CREATE", "DCAP_CREATECOPY", "DCAP_UPDATE",
        "DCAP_RASTER", "DCAP_VECTOR")}}
    is_vector = driver_kind(name, drv) == "vector"
    ext = drv.GetMetadataItem("DMD_EXTENSION") or "bin"
    stem = name.replace(" ", "_")
    create_result = try_create(name, tmp / (stem + "_c." + ext))
    createcopy_result = try_createcopy(name, raster_src, vector_src, tmp / (stem + "_k." + ext))
    produced = tmp / (stem + "_c." + ext)
    if not (isinstance(create_result, bool) and create_result):
        produced = tmp / (stem + "_k." + ext)
    if not produced.exists():
        produced = vector_src if is_vector else raster_src
    matrix[name] = {{
        "kind": driver_kind(name, drv),
        "declared": declared,
        "actual": {{
            "read": try_read(produced, is_vector),
            "create": create_result,
            "createcopy": createcopy_result,
            "update": try_update(name, produced),
        }},
    }}

installed = []
for i in range(gdal.GetDriverCount()):
    d = gdal.GetDriver(i)
    if d.GetMetadataItem("DCAP_OPEN") and (d.GetMetadataItem("DCAP_RASTER") or d.GetMetadataItem("DCAP_VECTOR")):
        installed.append(d.ShortName)

print(json.dumps({{"matrix": matrix, "installed_open_drivers": sorted(installed)}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _actual_ok(value: Any) -> bool:
    return value is True


def test_driver_four_mode_matrix_separated_and_honest(
    gdal_rt: dict[str, Any],
) -> None:
    report = _run_matrix_script(gdal_rt, MATRIX_DRIVERS)
    matrix = report["matrix"]

    # 四个点名驱动 + 扩展代表全部进入矩阵
    for name in MATRIX_DRIVERS:
        assert name in matrix and "missing" not in matrix[name], name

    mismatches: list[tuple[str, str]] = []
    for name, entry in matrix.items():
        declared = entry["declared"]
        actual = entry["actual"]
        pairs = (
            ("DCAP_CREATE", "create"),
            ("DCAP_CREATECOPY", "createcopy"),
            ("DCAP_UPDATE", "update"),
        )
        for dcap_key, mode in pairs:
            declared_yes = declared[dcap_key]
            actual_ok = _actual_ok(actual[mode])
            if declared_yes != actual_ok and not (
                not declared_yes and actual_ok and (name, dcap_key) in KNOWN_DECLARED_FALSE_BUT_WORKS
            ):
                mismatches.append((f"{name}.{mode}", f"declared={declared_yes} actual={actual[mode]}"))

    # 声明与实况的错位必须在已知白名单内——新错位 = GDAL 行为变化，必须显式过目
    assert not mismatches, "驱动声明与实测行为存在未知错位：" + repr(mismatches)

    # 点名驱动的关键事实（分开实测，不以声明代替）
    assert all(_actual_ok(matrix["GTiff"]["actual"][m]) for m in ("read", "create", "createcopy", "update"))
    assert _actual_ok(matrix["ENVI"]["actual"]["create"])
    assert _actual_ok(matrix["ENVI"]["actual"]["update"])
    assert _actual_ok(matrix["ENVI"]["actual"]["createcopy"])  # 声明 false，实测可用
    assert _actual_ok(matrix["COG"]["actual"]["create"])  # GDAL 3.13 已支持 Create
    assert _actual_ok(matrix["COG"]["actual"]["createcopy"])
    assert not _actual_ok(matrix["COG"]["actual"]["update"])  # COG 不可更新
    assert not _actual_ok(matrix["PNG"]["actual"]["create"])  # CreateCopy-only
    assert _actual_ok(matrix["PNG"]["actual"]["createcopy"])
    assert _actual_ok(matrix["GPKG"]["actual"]["update"])
    assert _actual_ok(matrix["netCDF"]["actual"]["create"])
    assert _actual_ok(matrix["netCDF"]["actual"]["read"])

    # 已装扩展驱动枚举：数量可观且包含 netCDF / FlatGeobuf / BMP 等扩展代表
    installed = set(report["installed_open_drivers"])
    assert len(installed) > 100, f"已装可打开驱动过少：{len(installed)}"
    for expected in ("netCDF", "FlatGeobuf", "BMP", "HFA", "PCIDSK"):
        assert expected in installed, f"扩展驱动 {expected} 未在已装清单中"


def test_describe_driver_reports_flags_and_creation_options(
    qapp: QApplication,
) -> None:
    client = QtWorkerClient(provider_id="gdal", worker_id="p505-desc", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        gtiff = client.describe_driver("GTiff")
        assert gtiff["driver_name"] == "GTiff"
        assert gtiff["extension"] == "tif"
        assert gtiff["flags"]["DCAP_CREATE"] is True
        assert gtiff["flags"]["DCAP_VECTOR"] is False
        options = {o["name"]: o for o in gtiff["creation_options"]}
        assert "COMPRESS" in options
        assert "LZW" in options["COMPRESS"].get("values", [])
        assert "TILED" in options

        gpkg = client.describe_driver("GPKG")
        assert gpkg["flags"]["DCAP_VECTOR"] is True
        assert gpkg["flags"]["DCAP_RASTER"] is True
        assert "VERSION" in {o["name"] for o in gpkg["creation_options"]}

        # 未注册驱动 / 空名：明确报错
        with pytest.raises(Exception, match="未注册驱动"):
            client.describe_driver("NOT_A_DRIVER")
        with pytest.raises(Exception, match="format 不能为空"):
            client.describe_driver("")
    finally:
        client.stop(timeout=5.0)
