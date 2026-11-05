"""P4-01 第 28 轮：Rasterio worker 真实入口的参数契约回归。

第 27 轮复核指出的确定性缺陷：
`RasterioProviderAdapter` 的 validate/execute 未调用 `normalize_raster_params`。
直接向**真实 Rasterio worker** 提交 `srcWin/xRes/yRes` 仍 succeeded，
输出保持源 16×16，参数被静默忽略。

**上一轮测试为何没抓到**：`test_p4_capability_boundary.py` 的 `_run_task` 固定创建
GDAL 引擎/客户端，`test_rasterio_convert_rejects_grid_params` 只换了 capability_id
而没换 provider，拿到的是 GDAL 的 `unsupported_capability`；把断言放宽成「任意失败」
等于**没有验证 Rasterio 的参数拒绝路径**。

本文件的关键改进：
- **正确引擎路由**：`QtWorkerClient(provider_id="rasterio")` +
  `TaskSpec.engine.provider_id="rasterio"` + `runtime_id="rasterio-venv"`，
  实际发送到 Rasterio worker；
- **精确断言**：必须是 `invalid_parameters` 且消息含**具体被拒参数**，
  不得放宽为任意失败；`unsupported_capability` 不算通过。
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
    canonical_capability_id,
    normalize_raster_params,
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


@pytest.fixture(scope="module")
def source_raster(gdal_rt: dict, tmp_path_factory) -> Path:
    """16x16 Float32 合成栅格：值 = row*16 + col，EPSG:4326，NoData=-9999。"""
    out_dir = tmp_path_factory.mktemp("p401_rio_src")
    path = out_dir / "source_16x16.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = (np.arange(16, dtype=np.float32)[:, None] * 16 + np.arange(16, dtype=np.float32)[None, :])
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 16, 16, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 0.001, 0.0, 30.0, 0.0, -0.001])
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
b.SetNoDataValue(-9999.0)
ds = None
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


# ------------------------------------------------------------------ 工具
def _run_rasterio_task(
    qapp: QApplication,
    capability_id: str,
    parameters: dict,
    in_path: Path,
    out_path: Path,
    timeout: float = 30.0,
):
    """**真实 Rasterio worker**：provider_id 与 runtime_id 都指向 Rasterio。

    这是本文件与上一轮测试的关键差别——上一轮只换 capability_id 不换 provider，
    实际跑的是 GDAL worker，拿到的错误码并非 Rasterio 的参数校验结果。
    """
    spec = TaskSpec(
        task_id=f"rio-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="rasterio",
            runtime_id="rasterio-venv",
            expected_version="1.5.2",
        ),
        project_root=str(ROOT),
        workspace=str(out_path.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="rio-source",
                kind=DatasetKind.raster,
                uri=in_path.resolve().as_uri(),
            )
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
    client = QtWorkerClient(provider_id="rasterio", project_root=str(ROOT))
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
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'gt': list(ds.GetGeoTransform())}))"
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


def _assert_rejected(result, expected_params: list[str]) -> None:
    """精确断言：必须是 invalid_parameters，且消息含**具体被拒参数**。

    `unsupported_capability` **不算通过**——那说明根本没走到参数校验路径，
    无法证明 Rasterio 的参数契约生效。
    """
    assert result.status is TaskStatus.failed, f"应失败，实际 {result.status}"
    assert result.error is not None, "失败结果必须带错误信息"
    code = result.error.get("code")
    assert code == "invalid_parameters", (
        f"错误码应为 invalid_parameters（而非 unsupported_capability），实际 {code}；"
        f"message={result.error.get('message')}"
    )
    message = result.error.get("message", "")
    for name in expected_params:
        assert name in message, f"错误消息应指出被拒参数 {name}，实际：{message}"


# ------------------------------------- 1. Rasterio 拒绝不支持的参数
def test_rasterio_rejects_srcwin_xres_yres(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """真实 Rasterio worker 必须拒绝 srcWin/xRes/yRes（它不消费这些参数）。"""
    out = tmp_path / "rio_clip.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "srcWin": [1, 2, 3, 4], "xRes": 0.002, "yRes": 0.002},
        source_raster,
        out,
    )
    _assert_rejected(result, ["srcWin", "xRes", "yRes"])
    assert not out.exists(), "参数被拒时不得产出文件"


def test_rasterio_rejects_dst_srs(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """dstSRS 对 Rasterio convert 同样不被支持。"""
    out = tmp_path / "rio_crs.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "dstSRS": "EPSG:3857"},
        source_raster,
        out,
    )
    _assert_rejected(result, ["dstSRS"])
    assert not out.exists()


def test_rasterio_rejects_unknown_parameter(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """拼错的参数名也必须被拒（避免「参数没生效但任务成功」）。"""
    out = tmp_path / "rio_typo.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "srcWn": [0, 0, 4, 4]},
        source_raster,
        out,
    )
    _assert_rejected(result, ["srcWn"])
    assert not out.exists()


# ------------------------------------- 2. 无网格参数时正常转换
def test_rasterio_convert_without_grid_params_succeeds(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """不携带网格参数时，Rasterio 格式转换必须正常成功并产出成果。"""
    out = tmp_path / "rio_ok.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert out.exists(), "正常转换应产出成果"
    info = _inspect(gdal_rt, out)
    assert (info["w"], info["h"]) == (16, 16), f"转换后尺寸应为 16x16，实际 {info['w']}x{info['h']}"
    assert info["gt"][1] == pytest.approx(0.001), f"像元大小应保持 0.001，实际 {info['gt'][1]}"


# ------------------------------------- 3. 各支持别名都受同一规则
def _rasterio_supported_capabilities() -> list[str]:
    """从 adapter 读取已支持能力集合（不硬编码）。

    硬编码会在今后新增能力时静默漏测，导致「契约别名表缺项 → 绕过能力级校验」
    这类缺陷重现（正是第 27 轮复核发现的绕过路径）。
    """
    from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter

    return sorted(RasterioProviderAdapter.SUPPORTED_CAPABILITIES)


@pytest.mark.parametrize("capability_id", _rasterio_supported_capabilities())
def test_all_supported_rasterio_aliases_reject_grid_params(
    qapp, gdal_rt, source_raster, tmp_path, capability_id: str
) -> None:
    """SUPPORTED_CAPABILITIES 里的**全部**别名都必须受同一参数契约。

    不能因为契约别名表缺项就绕过检查（上一轮 `rasterio:copy` /
    `rasterio:api:copy` 就会落到「未知能力」分支而跳过能力级校验）。
    """
    out = tmp_path / f"rio_alias_{capability_id.replace(':', '_')}.tif"
    result = _run_rasterio_task(
        qapp,
        capability_id,
        {"format": "GTiff", "srcWin": [1, 2, 3, 4]},
        source_raster,
        out,
    )
    _assert_rejected(result, ["srcWin"])
    assert not out.exists()


def test_unknown_rasterio_capability_is_unsupported_not_invalid_params(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """未知能力仍是 unsupported_capability（与「已支持能力携带非法参数」区分）。"""
    out = tmp_path / "rio_unknown_cap.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:definitely_not_supported",
        {"format": "GTiff"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error is not None
    assert result.error.get("code") == "unsupported_capability", (
        f"未知能力应为 unsupported_capability，实际 {result.error.get('code')}"
    )
    assert not out.exists()


# ------------------------------------- 4. 拒绝后既有输出不被破坏
def test_rasterio_rejection_preserves_existing_output(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """参数被拒时既有目标文件不得被覆盖或破坏。"""
    out = tmp_path / "rio_existing.tif"
    out.write_bytes(b"original content marker")

    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "srcWin": [1, 2, 3, 4]},
        source_raster,
        out,
    )
    _assert_rejected(result, ["srcWin"])
    assert out.read_bytes() == b"original content marker", "既有输出被破坏"


def test_rasterio_worker_still_usable_after_rejection(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """同一 worker 在拒绝之后仍能正常转换（拒绝不得毒化 worker 状态）。"""
    bad_out = tmp_path / "rio_after_bad.tif"
    result = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "xRes": 0.002},
        source_raster,
        bad_out,
    )
    _assert_rejected(result, ["xRes"])
    assert not bad_out.exists()

    good_out = tmp_path / "rio_after_good.tif"
    result2 = _run_rasterio_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff"},
        source_raster,
        good_out,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    assert good_out.exists(), "拒绝之后的正常转换应成功"


# ------------------------------------- 5. 契约层面的别名对齐
def test_all_supported_rasterio_capabilities_are_registered_in_contract() -> None:
    """契约必须登记 RasterioProviderAdapter.SUPPORTED_CAPABILITIES 的全部能力。

    否则未登记的会落到「未知能力」分支而跳过能力级校验。
    """
    from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter

    for cid in RasterioProviderAdapter.SUPPORTED_CAPABILITIES:
        canonical = canonical_capability_id(cid)
        assert canonical is not None, f"契约别名表缺少能力 {cid}"
        assert canonical == "rasterio:convert", f"能力 {cid} 应映射到 rasterio:convert"


def test_rasterio_contract_rejects_unsupported_params_directly() -> None:
    """契约层直接验证：Rasterio 能力不消费几何参数（与 adapter 无关的独立校验）。"""
    for params in (
        {"srcWin": [1, 2, 3, 4]},
        {"xRes": 0.002},
        {"yRes": 0.002},
        {"dstSRS": "EPSG:3857"},
    ):
        with pytest.raises(ParameterError):
            normalize_raster_params(params, capability_id="rasterio:convert")
    # 非几何参数必须放行
    normalize_raster_params({"format": "GTiff"}, capability_id="rasterio:convert")
