"""P8-01 多维数据树与二维切片端到端验收（真实 QGIS GDAL worker）。

验收标准（BACKLOG P8-01「合成 netCDF/HDF5 的 time/depth/x/y、单位和切片
像元正确」，对齐计划 §9.3/A11）：
- **多维数据树**（probe_multidim 查询通道）：group/attributes/dimensions/
  arrays——dims 含 has_coordinate_array；数组含 dims/shape/unit/scale/
  offset/srs；**HDF5 writer 本运行时缺失**（如实边界），容器按
  netCDF/CF-multidim 验收；
- **二维切片**（gdal:mdim_slice，TaskSpec 通道）：非地理维钉死 → rows/cols
  原序；像元 = raw 原值并独立重开数值断言（[[12,13,14],[15,16,17]]）；
- **单位/scale/offset/NoData/CRS 保留**：sidecar + GTiff band 元数据；
  无参考数组不编造 CRS；
- 拒绝路径：未知数组/未知维/钉死数量错误/越界索引，全部不落盘。

NetCDF **创建**限制（P3-02 定档，如实践守）：中文/空格路径不可写、只能
「chdir 到目标目录 + 相对文件名」创建（读取不受限）；测试装置遵守同一约定。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
)
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


def _make_multidim_nc(gdal_rt: dict[str, Any], out_dir: Path) -> Path:
    """合成 netCDF/CF 多维容器（time=2, depth=2, y=2, x=3）。

    - temp(time,depth,y,x)：值 = arange(24)；units=Kelvin、scale_factor=0.5、
      add_offset=1.0；**无 SpatialRef**（验证不编造地理参考）；
    - sst(time,depth,y,x)：值 = arange(1000..1023)；**SetSpatialRef(EPSG:4326)**
      （验证 CRS 保留）；
    - time/depth/y/x 坐标数组本位。
    netCDF 创建只能 chdir 到目标目录 + 相对文件名（P3-02 定档）。
    """
    script = f"""
import os
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
cwd = os.getcwd()
try:
    os.chdir(r'{out_dir}')
    nc = gdal.GetDriverByName('netCDF')
    ds = nc.CreateMultiDimensional('mdim_p8.nc')
    rg = ds.GetRootGroup()
    names = ('time', 'depth', 'y', 'x')
    sizes = (2, 2, 2, 3)
    dims = {{}}
    for n, s in zip(names, sizes):
        dims[n] = rg.CreateDimension(n, '', '', s)
    edt64 = gdal.ExtendedDataType.Create(gdal.GDT_Float64)
    edt32 = gdal.ExtendedDataType.Create(gdal.GDT_Float32)
    edtstr = gdal.ExtendedDataType.CreateString(16)
    for name, data in (
        ('time', np.array([0.0, 1.0], dtype=np.float64)),
        ('depth', np.array([5.0, 10.0], dtype=np.float64)),
        ('y', np.array([35.0, 34.5], dtype=np.float64)),
        ('x', np.array([114.0, 114.1, 114.2], dtype=np.float64)),
    ):
        a = rg.CreateMDArray(name, (dims[name],), edt64)
        a.Write(data)
    def _plant(arr_name, data, with_srs):
        a = rg.CreateMDArray(arr_name, (dims['time'], dims['depth'], dims['y'], dims['x']), edt32)
        a.CreateAttribute('units', [], edtstr).Write('Kelvin' if arr_name == 'temp' else 'degC')
        a.CreateAttribute('scale_factor', [], gdal.ExtendedDataType.Create(gdal.GDT_Float64)).Write(0.5)
        a.CreateAttribute('add_offset', [], gdal.ExtendedDataType.Create(gdal.GDT_Float64)).Write(1.0)
        if with_srs:
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            a.SetSpatialRef(srs)
        a.Write(data)
    _plant('temp', np.arange(24, dtype=np.float32).reshape(2, 2, 2, 3), False)
    _plant('sst', (np.arange(1000, 1024, dtype=np.float32)).reshape(2, 2, 2, 3), True)
    ds = None
finally:
    os.chdir(cwd)
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return out_dir / "mdim_p8.nc"


def _dump_gtiff(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    """QGIS python 独立重开 GTiff：尺寸/dtype/像元值/SRS 授权码/GT。"""
    script = f"""
import json
import numpy as np
from osgeo import gdal
src = gdal.Open({str(path)!r}, gdal.GA_ReadOnly)
band = src.GetRasterBand(1)
arr = band.ReadAsArray()
srs = src.GetSpatialRef()
print(json.dumps({{
  "width": src.RasterXSize, "height": src.RasterYSize,
  "dtype": str(arr.dtype),
  "values": arr.tolist(),
  "nodata": band.GetNoDataValue(),
  "scale": band.GetScale(),
  "offset": band.GetOffset(),
  "unittype": band.GetUnitType(),
  "crs_epsg": (srs.GetAuthorityCode(None) if srs is not None else None),
  "geotransform": src.GetGeoTransform(),
}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture()
def dispatch(qapp: QApplication):
    """真实 Worker 分发器（gdal provider，一任务一 client）。"""
    clients: list[QtWorkerClient] = []

    def _dispatch(spec: dict[str, Any]) -> Any:
        client = QtWorkerClient(
            provider_id="gdal",
            worker_id=f"mdim-{uuid.uuid4().hex[:8]}",
            project_root=str(ROOT),
        )
        clients.append(client)
        spec_obj = TaskSpec(
            task_id=spec["task_id"],
            attempt=spec["attempt"],
            capability_id=spec["capability_id"],
            descriptor_revision=spec["descriptor_revision"],
            engine=EngineConstraint(**spec["engine"]),
            project_root=spec["project_root"],
            workspace=spec.get("workspace") or "",
            inputs={
                key: [DatasetRef.model_validate(v) for v in refs]
                if isinstance(refs, list)
                else DatasetRef.model_validate(refs)
                for key, refs in (spec["inputs"] or {}).items()
            },
            outputs=[OutputPlan.model_validate(o) for o in spec["outputs"]],
            parameters=spec.get("parameters") or {},
        )
        results: list[Any] = []
        errors: list[tuple[str, str]] = []
        client.sig_result.connect(results.append)
        client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
        try:
            client.start(timeout=30.0)
            client.submit_task_async(spec_obj, timeout=90.0)
            deadline = time.monotonic() + 90.0
            while not results and not errors and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
            assert not errors, errors
            assert results, "任务未产生结果"
            return results[0]
        finally:
            client.stop(timeout=5.0)

    yield _dispatch

    for stale in clients:
        if stale.is_alive:
            stale.stop(timeout=5.0)


def _spec_dict(in_path: Path, out_path: Path, params: dict[str, Any]) -> dict[str, Any]:
    return TaskSpec.model_validate(
        {
            "task_id": f"mdim-{uuid.uuid4().hex[:8]}",
            "attempt": 1,
            "capability_id": "gdal:mdim_slice",
            "descriptor_revision": "1.0",
            "engine": EngineConstraint(
                provider_id="gdal",
                runtime_id="gdal-qgis",
                expected_version="3.13.3",
            ).model_dump(mode="json"),
            "project_root": str(ROOT),
            "workspace": str(out_path.parent),
            "inputs": {
                "input": DatasetRef(
                    dataset_id="mdim-in",
                    kind=DatasetKind.raster,
                    uri=in_path.resolve().as_uri(),
                ).model_dump(mode="json")
            },
            "outputs": [
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_path.resolve().as_uri(),
                    overwrite=True,
                ).model_dump(mode="json")
            ],
            "parameters": params,
        }
    ).model_dump(mode="json")


# ========================================================= 1. 多维数据树
@pytest.mark.integration
def test_multidim_tree_probe(gdal_rt: dict[str, Any], qapp: QApplication) -> None:
    out_dir = ROOT / "work" / "mdim_p8"
    out_dir.mkdir(parents=True, exist_ok=True)
    nc = _make_multidim_nc(gdal_rt, out_dir)

    client = QtWorkerClient(provider_id="gdal", worker_id="p8-tree", project_root=str(ROOT))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        tree = client.probe_multidim(nc, timeout=25.0)
    finally:
        client.stop(timeout=5.0)

    assert tree["group"] == "/"
    dims = {d["name"]: d["size"] for d in tree["dims"]}
    assert dims == {"time": 2, "depth": 2, "y": 2, "x": 3}
    for d in tree["dims"]:
        assert d["has_coordinate_array"] is True
    arrays = {a["name"]: a for a in tree["arrays"]}
    for name in ("time", "depth", "y", "x", "temp", "sst"):
        assert name in arrays, f"数组 {name} 应在树中"
    temp = arrays["temp"]
    assert temp["dims"] == ["time", "depth", "y", "x"]
    assert temp["shape"] == [2, 2, 2, 3]
    assert temp["unit"] == "Kelvin"
    assert temp["scale"] == pytest.approx(0.5)
    assert temp["offset"] == pytest.approx(1.0)
    assert temp["srs_wkt"] is None  # temp 未设参考：如实为 None（不编造）
    sst = arrays["sst"]
    assert sst["srs_wkt"] and "WGS 84" in sst["srs_wkt"], "sst 应带 EPSG:4326 参考"
    assert arrays["x"]["shape"] == [3]


# ======================================== 2. 二维切片 + 单位/像元正确
def test_multidim_slice_units_and_pixels(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    out_dir = tmp_path / "nc"
    out_dir.mkdir(parents=True, exist_ok=True)
    nc = _make_multidim_nc(gdal_rt, out_dir)
    out = tmp_path / "slice_t1_d0.tif"
    spec = _spec_dict(
        nc, out, {"array": "temp", "dimSelect": "time=1,depth=0", "format": "GTiff"}
    )
    result = dispatch(spec)
    assert result.status.value == "succeeded", result.error
    assert out.is_file()

    dump = _dump_gtiff(gdal_rt, out)
    # 像元 = raw 原值：arange(24).reshape(2,2,2,3)[1][0]（时间=1、深度=0）
    assert dump["values"] == [[12.0, 13.0, 14.0], [15.0, 16.0, 17.0]]
    assert dump["width"] == 3 and dump["height"] == 2
    # 单位/热元数据经 band 保留（GTiff band scale/offset）
    assert dump["scale"] == pytest.approx(0.5)
    assert dump["offset"] == pytest.approx(1.0)
    assert dump["unittype"] == "Kelvin"
    # temp 无 SpatialRef：输出不带 CRS（sidecar 如实 georeferenced=false）
    assert dump["crs_epsg"] is None
    sidecar_path = out.with_name(out.name + ".mdim_slice.json")
    assert sidecar_path.is_file()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["source"]["array"] == "temp"
    selected = {s["dim"]: s for s in sidecar["selected"]}
    assert selected["time"]["index"] == 1 and selected["time"]["value"] == pytest.approx(1.0)
    assert selected["depth"]["index"] == 0 and selected["depth"]["value"] == pytest.approx(5.0)
    assert sidecar["remaining_dims"] == [
        {"dim": "y", "size": 2},
        {"dim": "x", "size": 3},
    ]
    assert sidecar["coords"] == {
        "y": [35.0, 34.5],
        "x": [114.0, 114.1, 114.2],
    }
    assert sidecar["unit"] == "Kelvin"
    assert sidecar["scale"] == pytest.approx(0.5)
    assert sidecar["offset"] == pytest.approx(1.0)
    assert sidecar["georeferenced"] is False
    assert sidecar["value_semantics"].startswith("raw")


# ==================================== 3. CRS 保留（gridded 数组）+ 无参考分离
def test_multidim_slice_preserves_crs(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    out_dir = tmp_path / "nc2"
    out_dir.mkdir(parents=True, exist_ok=True)
    nc = _make_multidim_nc(gdal_rt, out_dir)
    out = tmp_path / "sst_slice.tif"
    spec = _spec_dict(
        nc, out, {"array": "sst", "dimSelect": {"time": 0, "depth": 1}}
    )
    result = dispatch(spec)
    assert result.status.value == "succeeded", result.error
    dump = _dump_gtiff(gdal_rt, out)
    # sst raw：arange(1000..1023).reshape(2,2,2,3)[0][1]
    assert dump["values"] == [[1006.0, 1007.0, 1008.0], [1009.0, 1010.0, 1011.0]]
    assert dump["crs_epsg"] == "4326", "SetSpatialRef 的数组应在切片输出保留 CRS"
    assert dump["unittype"] == "degC"
    sidecar = json.loads(
        (out.with_name(out.name + ".mdim_slice.json")).read_text(encoding="utf-8")
    )
    assert sidecar["georeferenced"] is True
    assert "4326" in sidecar["crs_wkt"]


# ======================================== 4. 拒绝矩阵（全部不落盘）
def test_multidim_slice_rejection_paths(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    out_dir = tmp_path / "nc3"
    out_dir.mkdir(parents=True, exist_ok=True)
    nc = _make_multidim_nc(gdal_rt, out_dir)
    out = tmp_path / "should_not_exist.tif"
    sidecar = out.with_name(out.name + ".mdim_slice.json")

    cases = [
        (
            {"dimSelect": "time=1,depth=0"},  # array 缺失
            "invalid_parameters",
        ),
        ({"array": "ghost", "dimSelect": "time=1,depth=0"}, "mdim_error"),
        ({"array": "temp", "dimSelect": "pressure=1,depth=0"}, "mdim_error"),
        ({"array": "temp", "dimSelect": "time=1"}, "mdim_error"),  # 钉死数量不足
        ({"array": "temp", "dimSelect": "time=9,depth=0,y=0,x=0"}, "mdim_error"),  # 越界
        ({"array": "temp", "dimSelect": "time=1.5,depth=0"}, "invalid_parameters"),
        ({"array": "time", "dimSelect": "time=0"}, "mdim_error"),  # rank<3
    ]
    for params, expected_code in cases:
        result = dispatch(_spec_dict(nc, out, dict(params)))
        assert result.status.value == "failed", (params, result.status)
        assert result.error and result.error.get("code") == expected_code, (
            params,
            result.error,
        )
        assert not out.exists(), params
        assert not sidecar.exists(), params
