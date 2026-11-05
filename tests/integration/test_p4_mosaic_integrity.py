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
import time
import uuid
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.client import QtWorkerClient

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
