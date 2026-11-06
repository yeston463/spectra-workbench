"""P4-03 验收测试：元数据编辑（ENVI 头文件）、概览构建与格式选项。

原验收标准（docs/plan/02_里程碑与验收.md）：
「不覆盖未知字段；不改原始数据尺寸契约；多文件提交可恢复」

逐项验收（全部经真实 worker 执行，独立重开/读文本核对）：
- **不覆盖未知字段**：ENVI .hdr 追加自定义字段后经 editmeta 更新 NoData，
  自定义字段必须仍在（GDAL 原生重写会丢失——实测结论，平台用读-合并-写防住）；
- **不改原始数据尺寸契约**：概览构建后主影像尺寸不变（ops 层有红线断言，这里端到端复证）；
- **多文件提交可恢复**：ENVI 概览产生外部 .dat.ovr sidecar，与主文件成套提交；
  提交后整套可独立重开。
- in-place 语义：输出=输入；overwrite=false 被拒绝且源不被破坏。
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


def _make_envi(gdal_rt: dict, path: Path, with_custom_fields: bool = False) -> Path:
    """构造 ENVI 数据集；可选向 .hdr 追加未知字段（模拟历史软件写入）。"""
    dat = path
    script = f"""
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
arr = np.arange(64, dtype=np.float32).reshape(8, 8)
ds = gdal.GetDriverByName("ENVI").Create(r"{dat}", 8, 8, 1, gdal.GDT_Float32)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
ds.GetRasterBand(1).WriteArray(arr)
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True, text=True, timeout=120, env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"ENVI 构造失败：{proc.stderr}"
    hdr = dat.with_suffix(".hdr")
    if with_custom_fields:
        with hdr.open("a", encoding="latin-1") as f:
            f.write("custom_field_a = legacy_value_a\n")
            f.write("my_project_tag = production-2024\n")
    return dat


def _make_gtiff(gdal_rt: dict, path: Path) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
arr = np.arange(64, dtype=np.float32).reshape(8, 8)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 8, 8, 1, gdal.GDT_Float32)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
ds.GetRasterBand(1).WriteArray(arr)
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True, text=True, timeout=120, env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"GTiff 构造失败：{proc.stderr}"
    return path


def _run_inplace_task(qapp, capability_id, parameters, target: Path, timeout=60.0):
    """in-place 任务：inputs['inputs'] 单元素列表，输出=输入。"""
    spec = TaskSpec(
        task_id=f"p403-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "inputs": [
                DatasetRef(
                    dataset_id="src-0",
                    kind=DatasetKind.raster,
                    uri=target.resolve().as_uri(),
                )
            ]
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=target.resolve().as_uri(),
                driver="GTiff",
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p403-worker", project_root=str(ROOT))
    results, errors = [], []
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


def _inspect(gdal_rt: dict, path: Path) -> dict:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "ovs = []\n"
        "for i in range(b.GetOverviewCount()):\n"
        "    o = b.GetOverview(i)\n"
        "    ovs.append([o.XSize, o.YSize])\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'nodata': b.GetNoDataValue(), 'ovs': ovs,\n"
        "                  'driver': ds.GetDriver().ShortName}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True, text=True, timeout=120, env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _hdr_fields(hdr: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in hdr.read_text(encoding="latin-1").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------- 1. 不覆盖未知字段
def test_envi_unknown_fields_survive_editmeta(
    qapp, gdal_rt, tmp_path
) -> None:
    """editmeta 更新 ENVI NoData 后，.hdr 的自定义（未知）字段必须仍在。

    GDAL 原生重写会丢失全部未知字段（实测），平台用读-合并-写防住。
    """
    dat = _make_envi(gdal_rt, tmp_path / "legacy.dat", with_custom_fields=True)
    hdr = dat.with_suffix(".hdr")
    before = _hdr_fields(hdr)
    assert before.get("custom_field_a") == "legacy_value_a"
    assert before.get("my_project_tag") == "production-2024"

    result = _run_inplace_task(
        qapp, "gdal:editmeta", {"srcNodata": -9999.0}, dat
    )
    assert result.status is TaskStatus.succeeded, result.error

    after = _hdr_fields(hdr)
    # 未知字段必须保留
    assert after.get("custom_field_a") == "legacy_value_a", (
        f"未知字段 custom_field_a 被 GDAL 重写吞掉：{after}"
    )
    assert after.get("my_project_tag") == "production-2024", (
        f"未知字段 my_project_tag 被吞掉：{after}"
    )
    # 变更必须生效
    assert after.get("data ignore value") == "-9999", (
        f"NoData 未写入头文件：{after.get('data ignore value')}"
    )

    # 独立重开：NoData 生效、尺寸契约不变
    info = _inspect(gdal_rt, dat)
    assert info["nodata"] == pytest.approx(-9999.0)
    assert (info["w"], info["h"]) == (8, 8)


def test_gtiff_editmeta_sets_nodata(qapp, gdal_rt, tmp_path) -> None:
    """GTiff 的 editmeta：NoData 写入内部元数据，尺寸不变。"""
    tif = _make_gtiff(gdal_rt, tmp_path / "plain.tif")
    result = _run_inplace_task(qapp, "gdal:editmeta", {"srcNodata": 0.0}, tif)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, tif)
    assert info["nodata"] == pytest.approx(0.0)
    assert (info["w"], info["h"]) == (8, 8)


# ------------------------------------- 2. 不改原始数据尺寸契约（概览）
def test_envi_overview_sidecar_commit(qapp, gdal_rt, tmp_path) -> None:
    """ENVI 概览：外部 .ovr 与主文件**成套提交**（多文件提交可恢复）。

    实测：ENVI 构建概览产生外部 <dat>.ovr；staging 副本上构建后，
    .ovr 必须随主文件一起 commit 到目标目录，且整套可独立重开。
    """
    dat = _make_envi(gdal_rt, tmp_path / "ovr.dat")
    assert not dat.with_suffix(".dat.ovr").exists() and not dat.with_suffix(".hdr.ovr").exists()

    result = _run_inplace_task(
        qapp,
        "gdal:overview",
        {"levels": [2, 4], "resampleAlg": "NEAREST"},
        dat,
    )
    assert result.status is TaskStatus.succeeded, result.error

    ovr = dat.with_suffix(".dat.ovr")
    assert ovr.exists(), f"外部 .ovr 未随主文件提交：{ovr}"
    # 输出记录应来自实际成果
    assert result.outputs
    rec = result.outputs[0]
    assert (rec.get("width"), rec.get("height")) == (8, 8), (
        f"主影像尺寸契约被破坏：{rec.get('width')}x{rec.get('height')}"
    )

    # 整套可独立重开：主文件带概览
    info = _inspect(gdal_rt, dat)
    assert len(info["ovs"]) == 2, f"应有 2 级概览，实际 {len(info['ovs'])}"
    assert info["ovs"][0] == [4, 4], f"第一级应为 4x4，实际 {info['ovs'][0]}"
    assert info["ovs"][1] == [2, 2], f"第二级应为 2x2，实际 {info['ovs'][1]}"
    # .ovr 本身可独立打开（GTiff 格式）
    ovr_info = _inspect(gdal_rt, ovr)
    assert (ovr_info["w"], ovr_info["h"]) in ((4, 4), (8, 8)), "外部 .ovr 无法独立打开"


def test_gtiff_overview_embedded_and_size_contract(
    qapp, gdal_rt, tmp_path
) -> None:
    """GTiff 概览：内嵌主文件、主影像尺寸不变、倍率递增校验。"""
    tif = _make_gtiff(gdal_rt, tmp_path / "emb.tif")
    before_size = tif.stat().st_size
    result = _run_inplace_task(
        qapp,
        "gdal:overview",
        {"levels": [2, 4], "overviewCompress": "DEFLATE"},
        tif,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, tif)
    assert len(info["ovs"]) == 2
    assert (info["w"], info["h"]) == (8, 8), "主影像尺寸契约被破坏"
    # 内嵌（无外部 .ovr，文件变大）
    assert not tif.with_suffix(".tif.ovr").exists(), "GTiff 概览不应产生外部 .ovr"
    assert tif.stat().st_size > before_size, "内嵌概览后文件应变大"


def test_overview_requires_explicit_overwrite(qapp, gdal_rt, tmp_path) -> None:
    """in-place 更新必须显式 overwrite=true；false 时拒绝且**源不被破坏**。"""
    tif = _make_gtiff(gdal_rt, tmp_path / "safe.tif")
    marker = tif.read_bytes()[:64]

    spec_params = {"levels": [2]}
    spec = TaskSpec(
        task_id=f"p403-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id="gdal:overview",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(tif.parent),
        inputs={
            "inputs": [
                DatasetRef(
                    dataset_id="src-0",
                    kind=DatasetKind.raster,
                    uri=tif.resolve().as_uri(),
                )
            ]
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=tif.resolve().as_uri(),
                driver="GTiff",
                overwrite=False,
            )
        ],
        parameters=spec_params,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p403-ow", project_root=str(ROOT))
    results, errors = [], []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=30.0)
        deadline = time.monotonic() + 30.0
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
    finally:
        client.stop(timeout=5.0)
    assert results and results[0].status is TaskStatus.failed, "overwrite=false 必须被拒绝"
    assert not tif.with_suffix(".tif.ovr").exists(), "拒绝时不得产出概览"
    assert tif.read_bytes()[:64] == marker, "拒绝时源文件不得被破坏"


# ------------------------------------- 3. 契约与能力边界
def test_p403_contract_levels_validation() -> None:
    """契约层：levels 必须严格递增、每个≥2。"""
    from spectra_workbench.processing.raster_params import (
        ParameterError,
        normalize_raster_params,
    )

    ok = normalize_raster_params({"levels": [2, 4, 8]}, capability_id="gdal:overview")
    assert ok["levels"] == [2, 4, 8]
    for bad in ([2, 2], [4, 2], [1, 2], [2, 1.5]):
        with pytest.raises(ParameterError):
            normalize_raster_params({"levels": bad}, capability_id="gdal:overview")


def test_p403_capabilities_have_parameter_matrices() -> None:
    """editmeta/overview 必须有参数支持矩阵。"""
    from spectra_workbench.processing.raster_params import supported_params_for

    assert supported_params_for("gdal:editmeta") == frozenset({"srcNodata"})
    assert supported_params_for("gdal:overview") == frozenset(
        {"resampleAlg", "levels", "overviewCompress"}
    )
