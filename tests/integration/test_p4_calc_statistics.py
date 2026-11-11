"""P4-04 验收测试：表达式计算与直方图/基础统计（全部经真实 worker 执行）。

原验收标准（docs/plan/02_里程碑与验收.md）：
「解析错误、除零、整数溢出、mask、抽样/全量统计模式有证据」

证据分工：
- 解析错误/除零/整数溢出/mask 的**纯求值语义**见
  tests/unit/test_p4_expression_ops.py（32 项）；
- 本文件验证这些语义**穿过真实 GDAL 读写与事务提交后仍然成立**，
  以及 sampled/full 两种统计模式各自生成可核验的 JSON 报告。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
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


def _make_gtiff(
    gdal_rt: dict,
    path: Path,
    *,
    array_expr: str,
    nodata: float | None = None,
    geotransform: list[float] | None = None,
    projection: str | None = None,
) -> Path:
    """用 GDAL runtime 子进程造真实 GTiff（array_expr 是 numpy 数组表达式）。"""
    gt = geotransform or [100.0, 1.0, 0.0, 30.0, 0.0, -1.0]
    script = f"""
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
arr = np.array({array_expr}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
ds.SetProjection({str(projection or "")!r})
b = ds.GetRasterBand(1)
b.WriteArray(arr)
if {nodata!r} is not None:
    b.SetNoDataValue({nodata!r})
b.FlushCache()
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
    assert proc.returncode == 0, f"GTiff 构造失败：{proc.stderr}"
    return path


def _inspect(gdal_rt: dict, path: Path) -> dict[str, Any]:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open({str(path)!r})\n"
        "assert ds is not None, '产出无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "  'nodata': b.GetNoDataValue(), 'data': b.ReadAsArray().tolist(),\n"
        "  'gt': list(ds.GetGeoTransform()), 'proj': ds.GetProjectionRef(),\n"
        "  'driver': ds.GetDriver().ShortName}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _run_task(
    qapp: QApplication,
    *,
    capability_id: str,
    parameters: dict[str, Any],
    inputs: dict[str, Any],
    output: Path,
    output_kind: DatasetKind = DatasetKind.raster,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p404-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.1",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(output.parent),
        inputs=inputs,
        outputs=[
            OutputPlan(
                key="output",
                kind=output_kind,
                uri=output.resolve().as_uri(),
                driver="GTiff" if output_kind is DatasetKind.raster else None,
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p404-worker", project_root=str(ROOT))
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, f"任务上报异常：{errors}"
        assert results, "任务未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _calc_inputs(paths: list[Path]) -> dict[str, Any]:
    return {
        "inputs": [
            DatasetRef(dataset_id=f"src-{i}", kind=DatasetKind.raster, uri=p.resolve().as_uri())
            for i, p in enumerate(paths)
        ]
    }


def _stats_input(path: Path) -> dict[str, Any]:
    return {
        "input": DatasetRef(
            dataset_id="src-0", kind=DatasetKind.raster, uri=path.resolve().as_uri()
        )
    }


# ------------------------------------------------------------------ 表达式计算


def test_calc_two_inputs_writes_readable_raster(qapp, gdal_rt, tmp_path) -> None:
    """(A-B)/(A+B) 的真实栅格往返：值、NoData 标签、仿射/投影、成果记录。"""
    a = _make_gtiff(gdal_rt, tmp_path / "a.tif", array_expr="[[10,20],[30,40]]")
    b = _make_gtiff(gdal_rt, tmp_path / "b.tif", array_expr="[[5,10],[15,20]]")
    out = tmp_path / "ndvi.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "(A-B)/(A+B)", "format": "GTiff", "dstNodata": -9999.0},
        inputs=_calc_inputs([a, b]),
        output=out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["driver"] == "GTiff" and (info["w"], info["h"]) == (2, 2)
    assert info["nodata"] == pytest.approx(-9999.0)
    assert info["gt"] == pytest.approx([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
    for row in info["data"]:
        for value in row:
            assert value == pytest.approx(1.0 / 3.0, abs=1e-6)
    rec = result.outputs[0]
    assert rec["driver"] == "GTiff" and rec["bands"] == 1
    assert rec["calc"]["expression"] == "(A-B)/(A+B)"
    assert rec["calc"]["band_names"] == ["A", "B"]
    assert rec["calc"]["n_math_invalid"] == 0


def test_calc_single_input_is_allowed(qapp, gdal_rt, tmp_path) -> None:
    a = _make_gtiff(gdal_rt, tmp_path / "single.tif", array_expr="[[1,2],[3,4]]")
    out = tmp_path / "doubled.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "A*2"},
        inputs=_calc_inputs([a]),
        output=out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert _inspect(gdal_rt, out)["data"] == [[2.0, 4.0], [6.0, 8.0]]


def test_calc_division_by_zero_becomes_nodata_with_evidence(qapp, gdal_rt, tmp_path) -> None:
    a = _make_gtiff(gdal_rt, tmp_path / "num.tif", array_expr="[[1,2],[3,4]]")
    b = _make_gtiff(gdal_rt, tmp_path / "den.tif", array_expr="[[1,0],[2,0]]")
    out = tmp_path / "div.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "A/B", "dstNodata": -9999.0},
        inputs=_calc_inputs([a, b]),
        output=out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["data"][0][1] == pytest.approx(-9999.0)
    assert info["data"][1][1] == pytest.approx(-9999.0)
    assert info["data"][0][0] == pytest.approx(1.0)
    assert info["nodata"] == pytest.approx(-9999.0), "标签必须与无效区像元值一致"
    assert result.outputs[0]["calc"]["n_math_invalid"] == 2


def test_calc_input_nodata_mask_propagates(qapp, gdal_rt, tmp_path) -> None:
    a = _make_gtiff(
        gdal_rt,
        tmp_path / "masked.tif",
        array_expr="[[-9999,2],[3,4]]",
        nodata=-9999.0,
    )
    b = _make_gtiff(gdal_rt, tmp_path / "ones.tif", array_expr="[[1,1],[1,1]]")
    out = tmp_path / "masked_out.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "A+B", "dstNodata": -9999.0},
        inputs=_calc_inputs([a, b]),
        output=out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["data"][0][0] == pytest.approx(-9999.0)
    assert info["data"][0][1] == pytest.approx(3.0)
    assert result.outputs[0]["calc"]["n_input_invalid"] == 1


def test_calc_rejects_unsafe_expression(qapp, gdal_rt, tmp_path) -> None:
    a = _make_gtiff(gdal_rt, tmp_path / "unsafe.tif", array_expr="[[1,2]]")
    out = tmp_path / "unsafe_out.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "__import__('os').system('echo pwned')"},
        inputs=_calc_inputs([a]),
        output=out,
    )
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_expression"
    assert "白名单" in result.error["message"] or "只允许" in result.error["message"]
    assert not out.exists(), "被拒绝的表达式不能产出文件"


def test_calc_rejects_misaligned_inputs(qapp, gdal_rt, tmp_path) -> None:
    a = _make_gtiff(gdal_rt, tmp_path / "align_a.tif", array_expr="[[1,2],[3,4]]")
    b = _make_gtiff(gdal_rt, tmp_path / "align_b.tif", array_expr="[[1,2,3]]")
    out = tmp_path / "align_out.tif"
    result = _run_task(
        qapp,
        capability_id="gdal:calc",
        parameters={"calc": "A+B"},
        inputs=_calc_inputs([a, b]),
        output=out,
    )
    assert result.status is TaskStatus.failed
    assert "尺寸" in result.error["message"]
    assert not out.exists()


# ------------------------------------------------------------------ 统计报告


def _make_stats_raster(gdal_rt: dict, path: Path) -> Path:
    expr = "np.random.default_rng(7).uniform(0, 100, (150, 200)).astype(np.float32)"
    script = f"""
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
arr = {expr}
arr[10:20, 10:20] = -9999.0
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, 200, 150, 1, gdal.GDT_Float32)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
b = ds.GetRasterBand(1)
b.WriteArray(arr)
b.SetNoDataValue(-9999.0)
b.FlushCache()
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
    assert proc.returncode == 0, f"统计样本构造失败：{proc.stderr}"
    return path


def test_statistics_sampled_and_full_reports(qapp, gdal_rt, tmp_path) -> None:
    src = _make_stats_raster(gdal_rt, tmp_path / "stats_src.tif")
    sampled_path = tmp_path / "sampled.json"
    full_path = tmp_path / "full.json"

    r1 = _run_task(
        qapp,
        capability_id="gdal:statistics",
        parameters={
            "statisticsMode": "sampled",
            "histogramBins": 16,
            "maxSamples": 1000,
            "fullLimit": 50_000,
        },
        inputs=_stats_input(src),
        output=sampled_path,
        output_kind=DatasetKind.artifact,
    )
    r2 = _run_task(
        qapp,
        capability_id="gdal:statistics",
        parameters={
            "statisticsMode": "full",
            "histogramBins": 16,
            "maxSamples": 1000,
            "fullLimit": 50_000,
        },
        inputs=_stats_input(src),
        output=full_path,
        output_kind=DatasetKind.artifact,
    )
    assert r1.status is TaskStatus.succeeded, r1.error
    assert r2.status is TaskStatus.succeeded, r2.error

    sampled = json.loads(sampled_path.read_text(encoding="utf-8"))
    full = json.loads(full_path.read_text(encoding="utf-8"))
    assert sampled["mode"] == "sampled" and sampled["source"] == "sampled_grid"
    assert sampled["stride"] > 1 and sampled["sampled_is_estimate"] is True
    assert full["mode"] == "full" and full["source"] == "full_raster"
    assert full["stride"] == 1 and full["sampled_is_estimate"] is False
    assert full["pixel_count"] == 200 * 150
    assert sampled["bands"][0]["total_count"] < full["bands"][0]["total_count"]

    fb = full["bands"][0]
    assert fb["nodata"] == pytest.approx(-9999.0)
    assert fb["valid_count"] == 200 * 150 - 100
    hist = fb["histogram"]
    assert hist["bins"] == 16 and len(hist["counts"]) == 16
    assert sum(hist["counts"]) == fb["valid_count"]
    assert len(hist["edges"]) == 17
    # 原生近似/精确统计与直方图对照（探针实测两者确实不同）
    ref = fb["gdal_reference"]
    assert ref["pam_disabled"] is True
    assert len(ref["approx_stats"]) == 4 and len(ref["exact_stats"]) == 4
    assert ref["native_histogram_sum"] > 0

    for rec in (r1.outputs[0], r2.outputs[0]):
        assert rec["driver"] == "JSON" and rec["kind"] == "artifact"
        assert rec["bands"] == 1 and rec["histogram_bins"] == 16


def test_statistics_full_over_limit_is_refused(qapp, gdal_rt, tmp_path) -> None:
    src = _make_stats_raster(gdal_rt, tmp_path / "limit_src.tif")
    out = tmp_path / "never.json"
    result = _run_task(
        qapp,
        capability_id="gdal:statistics",
        parameters={
            "statisticsMode": "full",
            "histogramBins": 8,
            "fullLimit": 100,
        },
        inputs=_stats_input(src),
        output=out,
        output_kind=DatasetKind.artifact,
    )
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_parameters"
    assert "拒绝" in result.error["message"]
    assert not out.exists(), "被拒绝的统计不能产出半成品报告"


def test_statistics_invalid_mode_is_rejected_by_shared_contract(qapp, gdal_rt, tmp_path) -> None:
    src = _make_stats_raster(gdal_rt, tmp_path / "mode_src.tif")
    out = tmp_path / "invalid_mode.json"
    result = _run_task(
        qapp,
        capability_id="gdal:statistics",
        parameters={"statisticsMode": "quick", "histogramBins": 8},
        inputs=_stats_input(src),
        output=out,
        output_kind=DatasetKind.artifact,
    )
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_parameters"
    assert not out.exists()
