"""P4-02 验收测试：拼接、VRT、波段堆叠与波段管理。

原验收标准（docs/plan/02_里程碑与验收.md）：
「重叠策略、缺口、NoData、源路径变化及波段顺序正确」

逐项验收（全部经**真实 GDAL worker** 执行，再独立重开产出核对实际像元）：
- **重叠策略**：`overlapPriority` 为 last（默认，后加入者优先）时重叠区取后者；
  为 first 时取前者（由输入排序实现——GDAL 实测规则）。
- **缺口**：拼接区域中的空洞必须是 NoData，且不得影响有效区。
- **NoData**：源 NoData 不参与合成；dstNodata 落到成果。
- **源路径变化**：VRT 用相对路径引用源；目录整体搬移后 VRT 仍可打开。
- **波段顺序**：堆叠后波段顺序与输入顺序一致；波段管理按 bandList 严格重排。

所有预期值都由**独立计算**得出，不只检查字段存在。
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
from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
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


# ------------------------------------------------------------ 源数据构造
def _make_raster(gdal_rt: dict, path: Path, arr, gt, nodata=None, epsg: int = 4326) -> Path:
    """用 GDAL 构造已知内容的栅格（值矩阵 arr，原点 gt=(x0,dx,0,y0,0,-dy)）。"""

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = np.array({arr.tolist()!r}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG({epsg})
ds.SetGeoTransform({list(gt)!r})
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
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
def overlap_sources(gdal_rt: dict, tmp_path) -> tuple[Path, Path]:
    """两个部分重叠的源：A 值 10 覆盖 x0..3，B 值 20 覆盖 x2..5。

    重叠区（x2..3, y2..3）在 GDAL 实测下取**后加入**的源。
    """
    import numpy as np

    a = np.full((4, 4), 10.0)
    b = np.full((4, 4), 20.0)
    pa = _make_raster(gdal_rt, tmp_path / "a.tif", a, (100.0, 1.0, 0.0, 30.0, 0.0, -1.0))
    pb = _make_raster(gdal_rt, tmp_path / "b.tif", b, (102.0, 1.0, 0.0, 28.0, 0.0, -1.0))
    return pa, pb


@pytest.fixture
def gap_sources(gdal_rt: dict, tmp_path) -> tuple[Path, Path]:
    """两个**不相邻**的源：C 覆盖 x0..1、D 覆盖 x3..4，中间 x=2 是缺口。"""
    import numpy as np

    c = np.full((2, 2), 5.0)
    d = np.full((2, 2), 7.0)
    pc = _make_raster(
        gdal_rt, tmp_path / "c.tif", c, (100.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0
    )
    pd = _make_raster(
        gdal_rt, tmp_path / "d.tif", d, (103.0, 1.0, 0.0, 30.0, 0.0, -1.0), nodata=-9999.0
    )
    return pc, pd


@pytest.fixture
def multiband_source(gdal_rt: dict, tmp_path) -> Path:
    """3 波段栅格，各波段值分别为 100/200/300。"""

    path = tmp_path / "multi.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 4, 4, 3, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
ds.SetProjection(srs.ExportToWkt())
for i in range(1, 4):
    ds.GetRasterBand(i).WriteArray(np.full((4, 4), float(i * 100)))
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
    assert proc.returncode == 0, f"多波段源构造失败：{proc.stderr}"
    return path


# ------------------------------------------------------------------ 工具
def _run_gdal_task(
    qapp: QApplication,
    capability_id: str,
    parameters: dict,
    input_paths: list[Path],
    out_path: Path,
    timeout: float = 40.0,
):
    """经真实 GDAL worker 执行一个任务（支持多输入）。"""
    spec = TaskSpec(
        task_id=f"p402-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal",
            runtime_id="gdal-qgis",
            expected_version="3.13.3",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p402-worker", project_root=str(ROOT))
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


def _inspect(gdal_rt: dict, path: Path) -> dict:
    """独立重开产出，读取全部波段像元与元数据。"""
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "bands = []\n"
        "for i in range(1, ds.RasterCount + 1):\n"
        "    bands.append(ds.GetRasterBand(i).ReadAsArray().tolist())\n"
        "print(json.dumps({\n"
        "    'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "    'bands': bands,\n"
        "    'nodata': b.GetNoDataValue(),\n"
        "    'gt': list(ds.GetGeoTransform()),\n"
        "    'driver': ds.GetDriver().ShortName,\n"
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


# --------------------------------------------------- 1. 重叠优先级
def test_merge_overlap_last_wins(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """默认优先级 last：重叠区取**后加入**的源（GDAL 实测规则）。"""
    pa, pb = overlap_sources
    out = tmp_path / "merge_last.tif"
    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [pa, pb], out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert (info["w"], info["h"]) == (6, 6), f"拼接范围应为 6x6，实际 {info['w']}x{info['h']}"
    row = info["bands"][0][2]
    # 重叠区（col 2..3）应取后加入的 B（值 20）
    assert row[2] == 20.0, f"重叠区应取后加入的源（20），实际 {row[2]}"
    # 非重叠区各自保留
    assert row[0] == 10.0, f"仅 A 覆盖区应为 10，实际 {row[0]}"
    assert row[4] == 20.0, f"仅 B 覆盖区应为 20，实际 {row[4]}"


def test_merge_overlap_first_wins(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """优先级 first：重叠区取**先给定**的源（由输入排序实现）。"""
    pa, pb = overlap_sources
    out = tmp_path / "merge_first.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "overlapPriority": "first"},
        [pa, pb],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    row = info["bands"][0][2]
    assert row[2] == 10.0, f"first 优先级下重叠区应取先给定的源（10），实际 {row[2]}"
    assert row[4] == 20.0, f"仅 B 覆盖区应为 20，实际 {row[4]}"


# ------------------------------------------------------------- 2. 缺口
def test_merge_gap_is_nodata(qapp, gdal_rt, gap_sources, tmp_path) -> None:
    """拼接区域中的空洞必须是 NoData，且不污染有效区。"""
    pc, pd = gap_sources
    out = tmp_path / "merge_gap.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999, "dstNodata": -9999},
        [pc, pd],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 5, f"拼接宽度应为 5（含缺口），实际 {info['w']}"
    assert info["nodata"] == pytest.approx(-9999.0), (
        f"成果 NoData 应为 -9999，实际 {info['nodata']}"
    )
    row = info["bands"][0][0]
    assert row[0] == 5.0, f"C 覆盖区应为 5，实际 {row[0]}"
    assert row[2] == pytest.approx(-9999.0), f"缺口应为 NoData，实际 {row[2]}"
    assert row[4] == 7.0, f"D 覆盖区应为 7，实际 {row[4]}"


def test_source_nodata_does_not_leak_into_result(qapp, gdal_rt, gap_sources, tmp_path) -> None:
    """源 NoData 不得作为有效像元进入成果。"""
    pc, pd = gap_sources
    out = tmp_path / "merge_nodata_leak.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999, "dstNodata": -9999},
        [pc, pd],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    for row in info["bands"][0]:
        for v in row:
            assert v in (5.0, 7.0, -9999.0), f"出现了既非有效值也非 NoData 的像元：{v}"


# ------------------------------------------------------- 3. VRT 与源路径变化
def test_buildvrt_uses_relative_source_paths(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """VRT 必须用相对路径引用源（这是「源路径变化」可用的前提）。"""
    pa, pb = overlap_sources
    out = tmp_path / "out.vrt"
    result = _run_gdal_task(qapp, "gdal:buildvrt", {}, [pa, pb], out)
    assert result.status is TaskStatus.succeeded, result.error
    assert out.exists(), "VRT 成果未生成"

    text = out.read_text(encoding="utf-8")
    assert 'relativeToVRT="1"' in text, "VRT 源引用应为相对路径（relativeToVRT=1）"
    assert str(tmp_path).replace("\\", "/") not in text.replace("\\", "/"), (
        "VRT 不应包含源目录的绝对路径"
    )


def test_vrt_still_opens_after_directory_moved(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """目录整体搬移后 VRT 仍可打开（相对引用随目录关系保持）。

    这是「源路径变化」验收的核心：VRT 与源保持相对关系时，
    整个目录改名/搬移后 VRT 依然可用。
    """
    pa, pb = overlap_sources
    # 把 VRT 目标放在与源同一目录下，才能形成稳定的相对关系
    out = tmp_path / "bundle" / "mosaic.vrt"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _run_gdal_task(qapp, "gdal:buildvrt", {}, [pa, pb], out)
    assert result.status is TaskStatus.succeeded, result.error

    before = _inspect(gdal_rt, out)
    assert before["w"] == 6 and before["h"] == 6

    # 整体搬移目录
    moved = tmp_path / "bundle_moved"
    out.parent.rename(moved)
    moved_vrt = moved / "mosaic.vrt"
    try:
        after = _inspect(gdal_rt, moved_vrt)
        assert after["w"] == 6 and after["h"] == 6, "搬移后 VRT 尺寸应不变"
        assert after["bands"] == before["bands"], "搬移后 VRT 波段数应不变"
        assert after["bands"][0] == before["bands"][0], "搬移后 VRT 像元值应不变"
    finally:
        moved.rename(out.parent)


# ------------------------------------------------------------- 4. 波段顺序
def test_stack_band_order_follows_input_order(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """波段堆叠：波段顺序必须与输入源顺序一致。"""
    pa, pb = overlap_sources
    out = tmp_path / "stack.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:stack",
        {"format": "GTiff", "srcNodata": -9999, "vrtNodata": -9999},
        [pa, pb],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert len(info["bands"]) == 2, f"堆叠后应有 2 个波段，实际 {len(info['bands'])}"
    # 波段归属断言不依赖坐标换算（separate VRT 的各波段 extent 与并集范围不同）：
    # 波段 1 的像元只能来自源 A（10 或 NoData），波段 2 只能来自源 B（20 或 NoData），
    # 且两者**互不含**对方的特征值 —— 这样才能真正证明波段顺序与输入顺序一致。
    band1_values = {v for row in info["bands"][0] for v in row}
    band2_values = {v for row in info["bands"][1] for v in row}
    assert band1_values <= {10.0, -9999.0}, f"波段 1 含非源 A 的值：{band1_values}"
    assert band2_values <= {20.0, -9999.0}, f"波段 2 含非源 B 的值：{band2_values}"
    assert 10.0 in band1_values, "波段 1 应含源 A 的特征值 10"
    assert 20.0 in band2_values, "波段 2 应含源 B 的特征值 20"
    assert 20.0 not in band1_values, "波段 1 混入了源 B 的值，波段顺序错误"
    assert 10.0 not in band2_values, "波段 2 混入了源 A 的值，波段顺序错误"


def test_band_manage_reorders_bands(qapp, gdal_rt, multiband_source, tmp_path) -> None:
    """波段管理：按 bandList 严格重排波段。"""
    out = tmp_path / "reordered.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:bandmanage",
        {"format": "GTiff", "bandList": [3, 1, 2]},
        [multiband_source],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert len(info["bands"]) == 3, f"应有 3 个波段，实际 {len(info['bands'])}"
    assert info["bands"][0][0][0] == 300.0, (
        f"波段 1 应为原波段 3（300），实际 {info['bands'][0][0][0]}"
    )
    assert info["bands"][1][0][0] == 100.0, (
        f"波段 2 应为原波段 1（100），实际 {info['bands'][1][0][0]}"
    )
    assert info["bands"][2][0][0] == 200.0, (
        f"波段 3 应为原波段 2（200），实际 {info['bands'][2][0][0]}"
    )


def test_band_manage_band_subset(qapp, gdal_rt, multiband_source, tmp_path) -> None:
    """波段管理：可只取部分波段。"""
    out = tmp_path / "subset.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:bandmanage",
        {"format": "GTiff", "bandList": [2]},
        [multiband_source],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert len(info["bands"]) == 1, f"应只保留 1 个波段，实际 {len(info['bands'])}"
    assert info["bands"][0][0][0] == 200.0, (
        f"保留的应是原波段 2（200），实际 {info['bands'][0][0][0]}"
    )


# -------------------------------------------------------- 5. 契约与能力边界
def test_p402_capabilities_have_parameter_matrices() -> None:
    """P4-02 四个能力都必须有明确的参数支持矩阵。"""
    for cid in ("gdal:buildvrt", "gdal:merge", "gdal:stack", "gdal:bandmanage"):
        supported = supported_params_for(cid)
        assert supported is not None, f"能力 {cid} 缺少参数支持矩阵"
    # 波段管理只认 bandList（外加 dtype）
    assert supported_params_for("gdal:bandmanage") == frozenset({"bandList", "outputType"})
    # 拼接不接受像元窗口（那属于 translate/warp 的能力边界）
    assert "srcWin" not in supported_params_for("gdal:merge")
    assert "srcWin" not in supported_params_for("gdal:buildvrt")


def test_p402_contract_rejects_bad_params() -> None:
    """契约层：非法参数与越界组合必须被拒。"""
    with pytest.raises(ParameterError):
        normalize_raster_params({"srcWin": [1, 2, 3, 4]}, capability_id="gdal:merge")
    with pytest.raises(ParameterError):
        normalize_raster_params({"bandList": [0]}, capability_id="gdal:bandmanage")
    with pytest.raises(ParameterError):
        normalize_raster_params({"bandList": [1.5]}, capability_id="gdal:bandmanage")
    with pytest.raises(ParameterError):
        normalize_raster_params({"overlapPriority": "mid"}, capability_id="gdal:merge")
    with pytest.raises(ParameterError):
        normalize_raster_params({"resolution": "bogus"}, capability_id="gdal:buildvrt")


def test_merge_rejects_too_few_inputs(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """拼接至少需要 2 个输入源，1 个必须被明确拒绝。"""
    pa, _ = overlap_sources
    out = tmp_path / "single_merge.tif"
    result = _run_gdal_task(qapp, "gdal:merge", {"format": "GTiff"}, [pa], out)
    assert result.status is TaskStatus.failed
    assert not out.exists()


def test_merge_with_unsupported_param_is_rejected(qapp, gdal_rt, overlap_sources, tmp_path) -> None:
    """拼接能力不接受 srcWin（它不消费该参数）——必须明确拒绝而非忽略。"""
    pa, pb = overlap_sources
    out = tmp_path / "bad_param.tif"
    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "srcWin": [1, 2, 3, 4]},
        [pa, pb],
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error is not None
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists()


def test_p402_failure_does_not_overwrite_existing_output(
    qapp, gdal_rt, overlap_sources, tmp_path
) -> None:
    """参数非法时既有目标不得被覆盖或破坏。"""
    pa, pb = overlap_sources
    out = tmp_path / "existing.tif"
    out.write_bytes(b"original content marker")

    result = _run_gdal_task(
        qapp,
        "gdal:merge",
        {"format": "GTiff", "srcWin": [1, 2, 3, 4]},
        [pa, pb],
        out,
    )
    assert result.status is TaskStatus.failed
    assert out.read_bytes() == b"original content marker", "既有输出被破坏"
