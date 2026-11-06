"""P4-02 第 30 轮验收测试：成果完整性、NoData 语义、输入完整性与 relativePaths 开关。

第 29 轮复核指出的四类缺陷，本文件逐项固化：

1. **多文件成果提交不完整**：ENVI 只提交 .dat，头文件留在 staging，
   独立 GDAL 重开最终 .dat 报 "not recognized as being in a supported file format"。
2. **dstNodata 仅改变标签**：缺口像元仍是 -9999、mask 却标为有效（255），
   缺口变成有效像元。
3. **不兼容输入被 GDAL 静默跳过**：混合 CRS 时第二个源被跳过，任务仍 succeeded。
4. **relativePaths=False 被忽略**：最终 XML 仍全部是相对引用。

另覆盖复核要求并入的安全边界：多波段输入的堆叠波段语义、
实际驱动取自实际重开、拒绝时不破坏既有目标。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    CancellationPolicy,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.processing.manifest import (
    StagingManifest,
    load_manifest,
    recover_staging,
    save_manifest,
)
from spectra_workbench.providers.base import ExecutionContext
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.scheduler.scheduler import TaskScheduler

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


def _get_qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return _get_qapp()


@pytest.fixture(scope="module")
def gdal_rt() -> dict:
    return resolve_gdal_runtime(ROOT)


def _make_raster(gdal_rt: dict, path: Path, arr, gt, nodata=None, epsg: int = 4326, bands: int = 1):
    """构造已知内容的栅格。bands>1 时各波段值依次 +1000。"""
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = np.array({arr.tolist()!r}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", arr.shape[1], arr.shape[0], {bands}, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG({epsg})
ds.SetGeoTransform({list(gt)!r})
ds.SetProjection(srs.ExportToWkt())
for i in range(1, {bands} + 1):
    b = ds.GetRasterBand(i)
    b.WriteArray((arr + (i - 1) * 1000).astype(np.float32))
    {f"b.SetNoDataValue({nodata!r})" if nodata is not None else ""}
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"源栅格构造失败：{proc.stderr}"
    return path


@pytest.fixture
def two_gap_sources(gdal_rt: dict, tmp_path) -> tuple[Path, Path]:
    """C 覆盖 x0..1（值 5，NoData=-9999）、D 覆盖 x3..4（值 7），中间 x=2 是缺口。"""
    import numpy as np

    c = _make_raster(
        gdal_rt, tmp_path / "c.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )
    d = _make_raster(
        gdal_rt, tmp_path / "d.tif", np.full((2, 2), 7.0),
        (103.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )
    return c, d


# ------------------------------------------------------------------ 工具
def _run_gdal_task(qapp, capability_id, parameters, input_paths, out_path, timeout=40.0):
    spec = TaskSpec(
        task_id=f"p430-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(out_path.parent),
        inputs={
            "inputs": [
                DatasetRef(
                    dataset_id=f"src-{i}",
                    kind=DatasetKind.raster,
                    uri=p.resolve().as_uri(),
                )
                for i, p in enumerate(input_paths)
            ]
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=out_path.resolve().as_uri(),
                driver="GTiff",
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p430-worker", project_root=str(ROOT))
    results = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=20.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, f"任务上报异常：{errors}"
        assert results, f"任务在 {timeout}s 内未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _inspect(gdal_rt: dict, path: Path, extra: str = "") -> dict:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "mask = b.GetMaskBand().ReadAsArray()\n"
        "print(json.dumps({\n"
        "    'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "    'arr': b.ReadAsArray().tolist(),\n"
        "    'mask': mask.tolist(),\n"
        "    'nodata': b.GetNoDataValue(),\n"
        "    'mask_flags': int(b.GetMaskFlags()),\n"
        "    'GMF_NODATA': int(gdal.GMF_NODATA),\n"
        "    'driver': ds.GetDriver().ShortName,\n"
        f"    {extra}\n"
        "}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"GDAL 校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


# ------------------------------------- 1. 多文件成果提交完整性
def test_envi_output_commits_header_sidecar(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """ENVI 成果必须**同时**提交 .dat 与 .hdr，且最终 .dat 可被独立 GDAL 打开。

    修正前：只提交 .dat，头文件留在 staging，最终 .dat 报
    "not recognized as being in a supported file format"。
    """
    pc, pd = two_gap_sources
    out_dat = tmp_path / "envi_out.dat"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "ENVI", "srcNodata": -9999, "vrtNodata": -9999},
        [pc, pd],
        out_dat,
    )
    assert result.status is TaskStatus.succeeded, result.error

    assert out_dat.exists(), "ENVI 主文件未落盘"
    assert out_dat.with_suffix(".hdr").exists(), (
        "ENVI 头文件未随主文件一起提交（成果集合不完整）"
    )

    # 独立 GDAL 重开最终 .dat 必须成功（头文件就位才可识别）
    info = _inspect(gdal_rt, out_dat)
    assert info["w"] == 5, f"ENVI 成果宽度应为 5，实际 {info['w']}"
    assert info["driver"].upper() == "ENVI", f"驱动应为 ENVI，实际 {info['driver']}"


def test_envi_output_records_real_driver(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """输出记录中的驱动必须来自**实际重开**，不是调用者默认的 GTiff。"""
    pc, pd = two_gap_sources
    out_dat = tmp_path / "envi_drv.dat"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "ENVI", "srcNodata": -9999, "vrtNodata": -9999},
        [pc, pd],
        out_dat,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert result.outputs, "成功结果必须带输出记录"
    record = result.outputs[0]
    assert record.get("driver", "").upper() == "ENVI", (
        f"输出记录的驱动应为实际驱动 ENVI，实际 {record.get('driver')}"
    )
    assert record.get("width") == 5, f"输出记录宽度应为实际值 5，实际 {record.get('width')}"


def test_buildvrt_records_vrt_driver(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """BuildVRT 的实际驱动是 VRT，不得无条件登记为 GTiff。"""
    pc, pd = two_gap_sources
    out = tmp_path / "out.vrt"
    result = _run_gdal_task(qapp, "gdal:buildvrt", {}, [pc, pd], out)
    assert result.status is TaskStatus.succeeded, result.error
    assert result.outputs
    assert result.outputs[0].get("driver", "").upper() == "VRT", (
        f"BuildVRT 的驱动应为 VRT，实际 {result.outputs[0].get('driver')}"
    )


# ------------------------------------- 2. dstNodata 与无效像元/mask 一致性
def test_dst_nodata_zero_keeps_gap_invalid(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """dstNodata=0 时，缺口必须**仍是无效像元**（值 0 且 mask 标无效）。

    修正前：NoData 标签变成 0，但缺口像元仍是 -9999 且 mask 标为有效（255），
    缺口变成有效像元。
    """
    pc, pd = two_gap_sources
    out = tmp_path / "nodata0.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {
            "format": "GTiff",
            "srcNodata": -9999,
            "vrtNodata": -9999,
            "dstNodata": 0,
        },
        [pc, pd],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)

    # 三者必须一致：NoData 标签、实际无效像元值、mask
    assert info["nodata"] == 0.0, f"输出 NoData 应为 0，实际 {info['nodata']}"
    row = info["arr"][0]
    mask_row = info["mask"][0]
    assert row[2] == 0.0, f"缺口的实际值应被重映射为 0，实际 {row[2]}"
    assert mask_row[2] == 0, f"缺口在 mask 中必须标为无效（0），实际 {mask_row[2]}"
    # 有效区不得被破坏
    assert row[0] == 5.0, f"有效区应为 5，实际 {row[0]}"
    assert row[4] == 7.0, f"有效区应为 7，实际 {row[4]}"
    assert mask_row[0] == 255 and mask_row[4] == 255, "有效区 mask 应为 255"
    assert info["mask_flags"] & info["GMF_NODATA"], (
        f"掩膜标志应含 GMF_NODATA({info['GMF_NODATA']})，实际 {info['mask_flags']}"
    )


def test_valid_zero_value_conflict_is_rejected(qapp, gdal_rt, tmp_path) -> None:
    """有效区中**真实的 0 值**与输出 NoData=0 冲突时必须明确拒绝。

    这正是「按 mask 重映射」（而非按值比较）的意义：
    只有按 mask 定位无效像元，才不会把有效区的真实 0 值误当成无效；
    而当输出 NoData 值本身与有效值相同时，必须拒绝而非产出矛盾成果。
    """
    import numpy as np

    # 源：有效区含 0 值，缺口为 -9999
    src = _make_raster(
        gdal_rt, tmp_path / "z.tif", np.array([[0.0, 5.0], [7.0, 9.0]]),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )
    other = _make_raster(
        gdal_rt, tmp_path / "z2.tif", np.array([[11.0, 12.0], [13.0, 14.0]]),
        (102.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )
    out = tmp_path / "zero_valid.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999, "dstNodata": 0},
        [src, other],
        out,
    )
    # 公开策略：输出 NoData 标签必须等于无效像元值，而有效区中恰有真实 0 值时
    # 二者不可兼得（该 0 会被 NoData 语义误判为无效）→ 明确拒绝，不产出矛盾成果。
    assert result.status is TaskStatus.failed, "有效值与输出 NoData 冲突时必须拒绝"
    assert result.error is not None
    assert "冲突" in result.error.get("message", ""), (
        f"错误消息应说明冲突，实际 {result.error.get('message')}"
    )
    assert not out.exists(), "冲突时不得产出文件"


# ------------------------------------- 3. 输入完整性（不兼容源不得静默跳过）
def test_mixed_crs_sources_are_rejected(qapp, gdal_rt, tmp_path) -> None:
    """混合 CRS 的输入必须**明确拒绝**，不得静默跳过第二个源后报成功。

    修正前：第二个源被 GDAL 跳过，任务仍 succeeded，输出只有第一个源的内容。
    """
    import numpy as np

    wgs = _make_raster(
        gdal_rt, tmp_path / "wgs.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), epsg=4326,
    )
    web = _make_raster(
        gdal_rt, tmp_path / "web.tif", np.full((2, 2), 99.0),
        (11131949.0, 1000.0, 0.0, 0.0, 0.0, -1000.0), epsg=3857,
    )
    out = tmp_path / "mixed.tif"
    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [wgs, web], out)
    assert result.status is TaskStatus.failed, "混合 CRS 必须被拒绝"
    assert result.error is not None
    assert result.error.get("code") == "incompatible_sources", (
        f"错误码应为 incompatible_sources，实际 {result.error.get('code')}"
    )
    assert "CRS" in result.error.get("message", ""), "错误消息应指出 CRS 不一致"
    assert not out.exists(), "拒绝时不得产出文件"


def test_mixed_band_count_sources_are_rejected(qapp, gdal_rt, tmp_path) -> None:
    """波段结构不一致的输入必须被明确拒绝（否则多波段源被静默截断）。"""
    import numpy as np

    one = _make_raster(
        gdal_rt, tmp_path / "b1.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=1,
    )
    three = _make_raster(
        gdal_rt, tmp_path / "b3.tif", np.full((2, 2), 7.0),
        (102.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=3,
    )
    out = tmp_path / "mixed_bands.tif"
    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [one, three], out)
    assert result.status is TaskStatus.failed, "波段结构不一致必须被拒绝"
    assert result.error is not None
    assert result.error.get("code") == "incompatible_sources"
    assert not out.exists()


def test_rejection_does_not_overwrite_existing_output(qapp, gdal_rt, tmp_path) -> None:
    """拒绝时既有目标不得被覆盖或破坏。"""
    import numpy as np

    wgs = _make_raster(
        gdal_rt, tmp_path / "wgs.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), epsg=4326,
    )
    web = _make_raster(
        gdal_rt, tmp_path / "web.tif", np.full((2, 2), 99.0),
        (11131949.0, 1000.0, 0.0, 0.0, 0.0, -1000.0), epsg=3857,
    )
    out = tmp_path / "existing.tif"
    out.write_bytes(b"original content marker")

    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [wgs, web], out)
    assert result.status is TaskStatus.failed
    assert out.read_bytes() == b"original content marker", "既有输出被破坏"


# ------------------------------------- 4. relativePaths 开关
def test_relative_paths_false_keeps_absolute_references(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """relativePaths=False 时最终源引用必须是**绝对路径**。

    修正前：该开关被忽略，最终 XML 仍全部是相对引用。
    """
    pc, pd = two_gap_sources
    out = tmp_path / "abs.vrt"
    result = _run_gdal_task(
        qapp, "gdal:buildvrt", {"relativePaths": False}, [pc, pd], out
    )
    assert result.status is TaskStatus.succeeded, result.error
    text = out.read_text(encoding="utf-8")
    assert 'relativeToVRT="1"' not in text, (
        "绝对模式下不得保留相对引用：\n" + text[:800]
    )
    assert 'relativeToVRT="0"' in text, "绝对模式应写出 relativeToVRT=0"


def test_relative_paths_true_writes_relative_references(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """relativePaths=True（默认）时源引用是相对路径且相对最终目录。"""
    pc, pd = two_gap_sources
    out = tmp_path / "rel.vrt"
    result = _run_gdal_task(qapp, "gdal:buildvrt", {"relativePaths": True}, [pc, pd], out)
    assert result.status is TaskStatus.succeeded, result.error
    text = out.read_text(encoding="utf-8")
    assert 'relativeToVRT="1"' in text, "相对模式应写出 relativeToVRT=1"
    assert str(tmp_path).replace("\\", "/") not in text.replace("\\", "/"), (
        "相对模式不应包含源目录绝对路径"
    )


def test_absolute_mode_vrt_still_opens(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    pc, pd = two_gap_sources
    """绝对模式产出的 VRT 必须可正常打开（源引用未被破坏）。"""
    pc, pd = two_gap_sources
    out = tmp_path / "abs_open.vrt"
    result = _run_gdal_task(
        qapp, "gdal:buildvrt", {"relativePaths": False}, [pc, pd], out
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 5, f"绝对模式 VRT 宽度应为 5，实际 {info['w']}"


# ------------------------------------- 5. 多波段堆叠的实际波段语义
def test_stack_multiband_sources_order(qapp, gdal_rt, tmp_path) -> None:
    """多波段输入堆叠：每个源的**所有波段依次进入**，顺序与源顺序一致。

    实测 GDAL 3.13.3：separate=True 时 2 波段源 + 3 波段源 → 5 个波段
    （值序 [5, 1005, 7, 1007, 2007]），**不是**「每源一个波段」。
    """
    import numpy as np

    a = _make_raster(
        gdal_rt, tmp_path / "ma.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=2,
    )
    b = _make_raster(
        gdal_rt, tmp_path / "mb.tif", np.full((2, 2), 7.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=3,
    )
    out = tmp_path / "stack_mb.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:stack",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999},
        [a, b],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{out}')\n"
        "vals = [float(ds.GetRasterBand(i).ReadAsArray()[0][0]) for i in range(1, ds.RasterCount+1)]\n"
        "print(json.dumps({'bands': ds.RasterCount, 'vals': vals}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"GDAL 校验失败：{proc.stderr}"
    info = json.loads(proc.stdout.strip())

    assert info["bands"] == 5, f"2 波段源 + 3 波段源应得到 5 个波段，实际 {info['bands']}"
    assert info["vals"] == [5.0, 1005.0, 7.0, 1007.0, 2007.0], (
        f"波段顺序应与源顺序一致，实际 {info['vals']}"
    )


# =====================================================================
# 第 31 轮：成果事务完整性、NoData/mask/alpha、源集合与 VRT XML
# =====================================================================
def _make_masked_raster(gdal_rt: dict, path: Path, arr, gt, mask, nodata=-9999.0):
    """构造带显式 dataset mask 的栅格（mask 中 0 表示无效像元）。"""
    arr_literal = arr.tolist() if hasattr(arr, "tolist") else arr
    mask_literal = mask.tolist() if hasattr(mask, "tolist") else mask
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = np.array({arr_literal!r}, dtype=np.float32)
mask = np.array({mask_literal!r}, dtype=np.uint8)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform({list(gt)!r}); ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
b.SetNoDataValue({nodata!r})
b.CreateMaskBand(gdal.GMF_PER_DATASET)
b.GetMaskBand().WriteArray(mask)
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"带 mask 的源栅格构造失败：{proc.stderr}"
    return path


def _make_alpha_raster(gdal_rt: dict, path: Path, values, alpha):
    """构造 4 波段 RGBA 栅格；alpha 0 表示完全透明。"""
    values_literal = values.tolist() if hasattr(values, "tolist") else values
    alpha_literal = alpha.tolist() if hasattr(alpha, "tolist") else alpha
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
values = np.array({values_literal!r}, dtype=np.float32)
alpha = np.array({alpha_literal!r}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", values.shape[1], values.shape[0], 4, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform((100.0, 1.0, 0.0, 30.0, 0.0, -1.0)); ds.SetProjection(srs.ExportToWkt())
for i in range(1, 4):
    b = ds.GetRasterBand(i)
    b.WriteArray((values + (i - 1) * 1000).astype(np.float32))
ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_RedBand)
ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_GreenBand)
ds.GetRasterBand(3).SetColorInterpretation(gdal.GCI_BlueBand)
ab = ds.GetRasterBand(4)
ab.WriteArray(alpha)
ab.SetColorInterpretation(gdal.GCI_AlphaBand)
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"RGBA 源栅格构造失败：{proc.stderr}"
    return path


def _make_ungeoref_raster(gdal_rt: dict, path: Path, value: float) -> Path:
    """构造没有 GeoTransform/CRS 的 GTiff（BuildVRT 会跳过此类源）。"""
    script = f"""
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 2, 2, 1, gdal.GDT_Float32)
ds.GetRasterBand(1).WriteArray(np.full((2, 2), {value!r}, dtype=np.float32))
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"无地理参考源栅格构造失败：{proc.stderr}"
    return path


def _make_large_georef_raster(gdal_rt: dict, path: Path, size: int, value: float) -> Path:
    """构造大尺寸地理参考 GTiff（用于真实计算期间取消，不在测试进程内生成数组）。"""
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", {size}, {size}, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform((100.0, 0.001, 0.0, 30.0, 0.0, -0.001))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.full(({size}, {size}), {value!r}, dtype=np.float32))
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"大尺寸源栅格构造失败：{proc.stderr}"
    return path


def _inspect_all_bands(gdal_rt: dict, path: Path) -> dict:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open(r"{path}")
assert ds is not None, "产出无法打开"
out = {{"driver": ds.GetDriver().ShortName, "w": ds.RasterXSize, "h": ds.RasterYSize,
        "bands": ds.RasterCount, "arrays": [], "masks": [], "nodata": [], "ci": [], "flags": []}}
for i in range(1, ds.RasterCount + 1):
    b = ds.GetRasterBand(i)
    out["arrays"].append(b.ReadAsArray().tolist())
    out["masks"].append(b.GetMaskBand().ReadAsArray().tolist())
    out["nodata"].append(b.GetNoDataValue())
    out["ci"].append(int(b.GetColorInterpretation()))
    out["flags"].append(int(b.GetMaskFlags()))
print(json.dumps(out))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"GDAL 校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


# ------------------------------------- 6. ENVI 覆盖 / 事务失败与中断
def test_envi_overwrite_replaces_data_and_header_as_bundle(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """覆盖既有 ENVI 时，新 .dat 与新 .hdr 必须成套替换，不能新数据配旧头。"""
    import numpy as np

    out_dat = tmp_path / "reuse.dat"
    first = _run_gdal_task(
        qapp, "gdal:merge", {"format": "ENVI", "srcNodata": -9999, "vrtNodata": -9999},
        list(two_gap_sources), out_dat,
    )
    assert first.status is TaskStatus.succeeded, first.error
    assert _inspect(gdal_rt, out_dat)["w"] == 5

    # 新成果尺寸/内容完全不同；若旧 .hdr 残留，GDAL 会按旧 samples/lines 读取
    a = _make_raster(
        gdal_rt, tmp_path / "n1.tif",
        np.array([[42.0, 42.0, 42.0, 42.0]]), (200.0, 1.0, 0.0, 31.0, 0.0, -1.0),
        nodata=-9999.0,
    )
    b = _make_raster(
        gdal_rt, tmp_path / "n2.tif",
        np.array([[43.0, 43.0, 43.0, 43.0]]), (204.0, 1.0, 0.0, 31.0, 0.0, -1.0),
        nodata=-9999.0,
    )
    second = _run_gdal_task(
        qapp, "gdal:merge", {"format": "ENVI", "srcNodata": -9999, "vrtNodata": -9999},
        [a, b], out_dat,
    )
    assert second.status is TaskStatus.succeeded, second.error
    info = _inspect(gdal_rt, out_dat)
    assert (info["w"], info["h"]) == (8, 1), (
        f"覆盖后应按新头文件读取 8x1，实际 {info['w']}x{info['h']}（疑似新数据配旧头）"
    )
    assert info["driver"].upper() == "ENVI"
    assert out_dat.with_suffix(".hdr").exists()
    assert second.outputs and len(second.outputs[0].get("committed_files", [])) >= 2, (
        "输出记录应包含主文件与头文件的提交集合"
    )
    # staging 中不得残留旧成果文件
    staging = tmp_path / ".staging" / second.task_id
    if staging.exists():
        leftovers = [p.name for p in staging.iterdir() if p.name != "manifest.json"]
        assert leftovers == [], f"staging 残留成果文件：{leftovers}"


def test_envi_commit_failure_restores_old_data_and_header(tmp_path) -> None:
    """提交第二个文件（头文件）失败时，旧 .dat 与旧 .hdr 必须同时恢复。"""
    staging = tmp_path / "staging"
    staging.mkdir()
    staged_dat = staging / "new.dat"
    staged_hdr = staging / "new.hdr"
    staged_dat.write_bytes(b"NEW DATA")
    staged_hdr.write_bytes(b"NEW HEADER")
    target_dat = tmp_path / "out.dat"
    target_hdr = tmp_path / "out.hdr"
    target_dat.write_bytes(b"OLD DATA")
    target_hdr.write_bytes(b"OLD HEADER")

    ctx = ExecutionContext(task_id="t-envi-rollback", workspace=tmp_path, staging_dir=staging)
    real_move = __import__("shutil").move

    def flaky_move(src, dst):
        if Path(src).resolve() == staged_hdr.resolve() and Path(dst).resolve() == target_hdr.resolve():
            raise OSError("simulated header move failure")
        return real_move(src, dst)

    with (
        patch("spectra_workbench.providers.base.shutil.move", side_effect=flaky_move),
        pytest.raises(OSError, match="simulated header move failure"),
    ):
        ctx.commit_files([(staged_dat, target_dat), (staged_hdr, target_hdr)], overwrite=True)

    assert target_dat.read_bytes() == b"OLD DATA"
    assert target_hdr.read_bytes() == b"OLD HEADER"
    assert staged_dat.exists() and staged_hdr.exists(), "回滚后 staging 成果应保留"
    manifest = load_manifest(staging)
    assert manifest is not None and manifest.state == "failed"


def test_envi_interrupted_commit_recovery_restores_bundle(tmp_path) -> None:
    """模拟提交在 data 已安装、header 尚未安装时被强杀：恢复必须成套还原旧文件。"""
    staging = tmp_path / "staging"
    staging.mkdir()
    target_dat = tmp_path / "out.dat"
    target_hdr = tmp_path / "out.hdr"
    target_dat.write_bytes(b"NEW DATA")
    target_hdr.write_bytes(b"OLD HEADER")
    backup_dat = tmp_path / ".out.dat.bak_test"
    backup_hdr = tmp_path / ".out.hdr.bak_test"
    backup_dat.write_bytes(b"OLD DATA")
    backup_hdr.write_bytes(b"OLD HEADER")

    manifest = StagingManifest(
        task_id="t-envi-interrupted",
        attempt=1,
        state="committing",
        commit_pairs=[
            {"staged": str(staging / "new.dat"), "target": str(target_dat)},
            {"staged": str(staging / "new.hdr"), "target": str(target_hdr)},
        ],
        backups=[
            {"target": str(target_dat), "backup": str(backup_dat)},
            {"target": str(target_hdr), "backup": str(backup_hdr)},
        ],
        file_entries=[
            {
                "staged_path": str(staging / "new.dat"),
                "target_path": str(target_dat),
                "action": "replace",
                "initial_exists": True,
                "initial_size": len(b"OLD DATA"),
                "initial_sha256": None,
                "backup_path": str(backup_dat),
                "staged_size": len(b"NEW DATA"),
                "staged_sha256": None,
                "status": "installed",
            },
            {
                "staged_path": str(staging / "new.hdr"),
                "target_path": str(target_hdr),
                "action": "replace",
                "initial_exists": True,
                "initial_size": len(b"OLD HEADER"),
                "initial_sha256": None,
                "backup_path": str(backup_hdr),
                "staged_size": len(b"NEW HEADER"),
                "staged_sha256": None,
                "status": "backed_up",
            },
        ],
        staged_files=[
            {"path": str(staging / "new.dat"), "size": len(b"NEW DATA"), "sha256": "x"},
            {"path": str(staging / "new.hdr"), "size": len(b"NEW HEADER"), "sha256": "y"},
        ],
    )
    save_manifest(staging, manifest)
    recovery = recover_staging(staging)

    assert recovery["recovered"] is True
    assert target_dat.read_bytes() == b"OLD DATA", "旧数据未恢复"
    assert target_hdr.read_bytes() == b"OLD HEADER", "旧头文件未恢复"
    assert not backup_dat.exists() and not backup_hdr.exists(), "备份应被消耗"


# ------------------------------------- 7. BuildVRT 输入完整性
def test_buildvrt_rejects_mixed_crs(qapp, gdal_rt, tmp_path) -> None:
    """BuildVRT 的混合 CRS 源必须拒绝：GDAL 会静默跳过第二个源。"""
    import numpy as np

    wgs = _make_raster(
        gdal_rt, tmp_path / "wgs.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), epsg=4326,
    )
    web = _make_raster(
        gdal_rt, tmp_path / "web.tif", np.full((2, 2), 99.0),
        (11131949.0, 1000.0, 0.0, 0.0, 0.0, -1000.0), epsg=3857,
    )
    out = tmp_path / "mixed.vrt"
    result = _run_gdal_task(qapp, "gdal:buildvrt", {}, [wgs, web], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "incompatible_sources"
    assert "CRS" in result.error.get("message", "")
    assert not out.exists()


def test_buildvrt_rejects_mixed_band_structure(qapp, gdal_rt, tmp_path) -> None:
    """非 separate BuildVRT 遇到不同波段数必须拒绝，不能静默跳过多波段源。"""
    import numpy as np

    one = _make_raster(
        gdal_rt, tmp_path / "one.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=1,
    )
    three = _make_raster(
        gdal_rt, tmp_path / "three.tif", np.full((2, 2), 7.0),
        (102.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=3,
    )
    out = tmp_path / "bands.vrt"
    result = _run_gdal_task(qapp, "gdal:buildvrt", {}, [one, three], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "incompatible_sources"
    assert not out.exists()


def test_merge_rejects_ungeoreferenced_sources(qapp, gdal_rt, tmp_path) -> None:
    """无 GeoTransform 的源会被 BuildVRT 整体跳过，必须提前明确拒绝。"""
    a = _make_ungeoref_raster(gdal_rt, tmp_path / "nogt_a.tif", 5.0)
    b = _make_ungeoref_raster(gdal_rt, tmp_path / "nogt_b.tif", 7.0)
    out = tmp_path / "nogt.tif"
    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [a, b], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "incompatible_sources"
    assert "GeoTransform" in result.error.get("message", "")
    assert not out.exists()


def test_buildvrt_separate_multiband_keeps_each_band(qapp, gdal_rt, tmp_path) -> None:
    """buildvrt separate=True：每个源的所有波段依次进入，2+3 波段 → 5 波段。"""
    import numpy as np

    a = _make_raster(
        gdal_rt, tmp_path / "sep_a.tif", np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=2,
    )
    b = _make_raster(
        gdal_rt, tmp_path / "sep_b.tif", np.full((2, 2), 7.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), bands=3,
    )
    out = tmp_path / "sep.vrt"
    result = _run_gdal_task(
        qapp, "gdal:buildvrt", {"separate": True}, [a, b], out
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect_all_bands(gdal_rt, out)
    assert info["bands"] == 5
    assert [band[0][0] for band in info["arrays"]] == [5.0, 1005.0, 7.0, 1007.0, 2007.0]


def test_stack_separate_allows_ungeoreferenced_sources(qapp, gdal_rt, tmp_path) -> None:
    """separate=True 的堆叠不要求地理参考；无 GeoTransform 源仍应逐源成带。"""
    a = _make_ungeoref_raster(gdal_rt, tmp_path / "stack_nogt_a.tif", 5.0)
    b = _make_ungeoref_raster(gdal_rt, tmp_path / "stack_nogt_b.tif", 7.0)
    out = tmp_path / "stack_nogt.tif"
    result = _run_gdal_task(qapp, "gdal:stack", {"format": "GTiff"}, [a, b], out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect_all_bands(gdal_rt, out)
    assert info["bands"] == 2
    assert [band[0][0] for band in info["arrays"]] == [5.0, 7.0]


def test_all_unreadable_sources_rejected_and_existing_output_preserved(qapp, gdal_rt, tmp_path) -> None:
    """全部源不可读时必须失败，且不得破坏既有目标。"""
    bad1 = tmp_path / "bad1.tif"
    bad2 = tmp_path / "bad2.tif"
    bad1.write_bytes(b"not a raster")
    bad2.write_bytes(b"also not a raster")
    out = tmp_path / "existing.tif"
    out.write_bytes(b"ORIGINAL MARKER")

    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [bad1, bad2], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "incompatible_sources"
    assert out.read_bytes() == b"ORIGINAL MARKER"


# ------------------------------------- 8. relativePaths / XML 结构
def test_buildvrt_escaped_unicode_paths_relative_and_absolute(qapp, gdal_rt, tmp_path) -> None:
    """源路径含空格/中文/& 时必须按 XML 结构转义；相对/绝对模式都可读。"""
    import numpy as np

    weird = _make_raster(
        gdal_rt,
        tmp_path / "源 数据 & 1.tif",
        np.full((2, 2), 11.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0),
        nodata=-9999.0,
    )
    normal = _make_raster(
        gdal_rt, tmp_path / "normal.tif", np.full((2, 2), 22.0),
        (102.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )

    rel_out = tmp_path / "rel" / "escaped.vrt"
    rel_out.parent.mkdir()
    rel = _run_gdal_task(
        qapp, "gdal:buildvrt", {"relativePaths": True}, [weird, normal], rel_out
    )
    assert rel.status is TaskStatus.succeeded, rel.error
    rel_text = rel_out.read_text(encoding="utf-8")
    assert "源 数据 &amp; 1.tif" in rel_text, "& 必须按 XML 实体转义"
    assert "源 数据 & 1.tif" not in rel_text, "不得写出未转义的 &"
    assert 'relativeToVRT="1"' in rel_text
    rel_info = _inspect(gdal_rt, rel_out)
    assert rel_info["w"] == 4
    assert rel_info["arr"][0][0] == 11.0 and rel_info["arr"][0][3] == 22.0

    abs_out = tmp_path / "abs" / "escaped_abs.vrt"
    abs_out.parent.mkdir()
    absolute = _run_gdal_task(
        qapp, "gdal:buildvrt", {"relativePaths": False}, [weird, normal], abs_out
    )
    assert absolute.status is TaskStatus.succeeded, absolute.error
    abs_text = abs_out.read_text(encoding="utf-8")
    assert 'relativeToVRT="0"' in abs_text
    assert "源 数据 &amp; 1.tif" in abs_text
    abs_info = _inspect(gdal_rt, abs_out)
    assert abs_info["w"] == 4
    assert abs_info["arr"][0][0] == 11.0 and abs_info["arr"][0][3] == 22.0


# ------------------------------------- 9. 输出格式明确拒绝
def test_buildvrt_explicit_unsupported_format_rejected(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """显式要求 buildvrt 输出 GTiff 时必须拒绝，不能写 VRT 后按 GTiff 报成功。"""
    out = tmp_path / "should_not_exist.vrt"
    result = _run_gdal_task(
        qapp, "gdal:buildvrt", {"format": "GTiff"}, list(two_gap_sources), out
    )
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_output_format"
    assert not out.exists()


def test_merge_to_vrt_output_is_readable(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """merge 请求 VRT 输出时：committed VRT 可按最终目录解析源并读取像元。"""
    out = tmp_path / "merged.vrt"
    result = _run_gdal_task(
        qapp, "gdal:merge",
        {"format": "VRT", "srcNodata": -9999, "vrtNodata": -9999},
        list(two_gap_sources), out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert result.outputs and result.outputs[0].get("driver", "").upper() == "VRT"
    info = _inspect(gdal_rt, out)
    assert info["w"] == 5
    assert info["arr"][0][0] == 5.0 and info["arr"][0][4] == 7.0


def test_merge_unknown_output_format_rejected(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """未知输出格式必须在写盘前明确拒绝，既有目标不被破坏。"""
    out = tmp_path / "unknown.dat"
    out.write_bytes(b"ORIGINAL")
    result = _run_gdal_task(
        qapp, "gdal:merge", {"format": "NOT_A_FORMAT_XYZ"}, list(two_gap_sources), out
    )
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_output_format"
    assert out.read_bytes() == b"ORIGINAL"


def test_stale_envi_header_blocks_non_envi_rewrite(qapp, gdal_rt, two_gap_sources, tmp_path) -> None:
    """目标 out.dat 旁已有旧 .hdr 时，GTiff 改写必须拒绝，避免新数据配旧头。"""
    out = tmp_path / "stale.dat"
    out.write_bytes(b"OLD DATA MARKER")
    hdr = tmp_path / "stale.hdr"
    hdr.write_text("ENVI\nsamples = 2\nlines = 2\nbands = 1\n", encoding="utf-8")
    result = _run_gdal_task(
        qapp, "gdal:merge", {"format": "GTiff"}, list(two_gap_sources), out
    )
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "stale_header_conflict"
    assert out.read_bytes() == b"OLD DATA MARKER"
    assert "samples = 2" in hdr.read_text(encoding="utf-8")


# ------------------------------------- 10. 显式 mask / alpha 的 NoData 语义
def test_explicit_mask_gap_remapped_and_mask_consistent(qapp, gdal_rt, tmp_path) -> None:
    """显式 mask 的缺口：值重映射为 dstNodata=0、mask=0；有效区保持 255。"""
    import numpy as np

    masked = _make_masked_raster(
        gdal_rt,
        tmp_path / "masked.tif",
        np.array([[5.0, 5.0], [5.0, 5.0]]),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0),
        mask=[[255, 0], [255, 255]],
    )
    other = _make_raster(
        gdal_rt, tmp_path / "masked_other.tif", np.full((2, 2), 7.0),
        (102.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0,
    )
    out = tmp_path / "masked_out.tif"
    result = _run_gdal_task(
        qapp, "gdal:merge",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999, "dstNodata": 0},
        [masked, other], out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["nodata"] == 0.0
    assert info["arr"][0][1] == 0.0 and info["mask"][0][1] == 0
    assert info["arr"][0][0] == 5.0 and info["mask"][0][0] == 255
    assert info["arr"][0][3] == 7.0 and info["mask"][0][3] == 255


def test_alpha_gap_remapped_with_consistent_mask(qapp, gdal_rt, tmp_path) -> None:
    """RGBA 的 alpha=0 缺口：color 重映射为 0、显式 mask=0、alpha 保持 0；有效区不被破坏。"""
    alpha = _make_alpha_raster(
        gdal_rt,
        tmp_path / "rgba.tif",
        values=[[5.0, 5.0], [5.0, 5.0]],
        alpha=[[255, 0], [255, 255]],
    )
    out = tmp_path / "rgba_out.tif"
    result = _run_gdal_task(
        qapp, "gdal:merge",
        {"format": "GTiff", "dstNodata": 0},
        [alpha, alpha], out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect_all_bands(gdal_rt, out)
    assert info["bands"] == 4
    # 三个 color band 的缺口都被重映射，且 mask 标无效
    for band_idx in range(3):
        assert info["arrays"][band_idx][0][1] == 0.0, (
            f"color band {band_idx + 1} 缺口未重映射：{info['arrays'][band_idx]}"
        )
        assert info["masks"][band_idx][0][1] == 0, (
            f"color band {band_idx + 1} 缺口 mask 仍为有效：{info['masks'][band_idx]}"
        )
        assert info["arrays"][band_idx][0][0] == 5.0 + band_idx * 1000
        assert info["masks"][band_idx][0][0] == 255
        assert info["nodata"][band_idx] == 0.0
    # alpha 通道保持透明语义：缺口 alpha=0，有效区 alpha=255；alpha 不被当作数据值重映射
    assert info["ci"][3] == 6, "第 4 波段应保持 alpha color interpretation"
    assert info["arrays"][3][0] == [255.0, 0.0]
    assert info["arrays"][3][1] == [255.0, 255.0]


# ------------------------------------- 11. 真实计算期间取消
def test_merge_cancellation_during_computation_cleans_staging(qapp, gdal_rt, tmp_path) -> None:
    """merge 在真实 GDAL 计算期间取消：返回 cancelled、无最终成果、staging 无残留成果。"""
    size = 1200
    src_a = _make_large_georef_raster(gdal_rt, tmp_path / "cancel_a.tif", size, 1.0)
    src_b = _make_large_georef_raster(gdal_rt, tmp_path / "cancel_b.tif", size, 2.0)
    workspace = tmp_path / "workspace_cancel"
    out = tmp_path / "cancel_merge.tif"

    spec = TaskSpec(
        task_id="t-p402-cancel",
        attempt=1,
        capability_id="gdal:merge",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(workspace),
        inputs={
            "inputs": [
                DatasetRef(dataset_id="a", kind=DatasetKind.raster, uri=src_a.as_uri()),
                DatasetRef(dataset_id="b", kind=DatasetKind.raster, uri=src_b.as_uri()),
            ]
        },
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out.as_uri(), overwrite=True)],
        cancellation=CancellationPolicy(cooperative_timeout_seconds=5.0, allow_process_termination=True),
        parameters={"format": "GTiff"},
    )
    sched = TaskScheduler(project_root=ROOT, max_concurrency=1)
    cancel_triggered = threading.Event()

    def _on_progress(handle, ratio, phase, _msg):
        if (
            handle.task_id == spec.task_id
            and phase == "processing"
            and ratio is not None
            and ratio >= 0.20
            and not cancel_triggered.is_set()
        ):
            cancel_triggered.set()
            sched.cancel(spec.task_id)

    sched.on_progress.append(_on_progress)
    sched.start()
    try:
        handle = sched.submit(spec)
        deadline = time.monotonic() + 30.0
        while not handle.is_terminal and time.monotonic() < deadline:
            time.sleep(0.05)
        assert cancel_triggered.is_set(), "未能在真实 GDAL 计算中触发取消"
        assert handle.status is TaskStatus.cancelled
        assert handle.final_result is not None and handle.final_result.status is TaskStatus.cancelled
        assert not out.exists(), "取消的任务不得提交最终成果"
        staging = workspace / ".staging" / spec.task_id
        if staging.exists():
            leftovers = [p.name for p in staging.iterdir() if p.name != "manifest.json"]
            assert leftovers == [], f"取消后 staging 残留临时成果：{leftovers}"
    finally:
        sched.stop()
