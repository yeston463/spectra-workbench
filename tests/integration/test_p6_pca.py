"""P6-03 PCA 正/逆变换端到端：真 worker + 独立 GDAL 重开验收。

数值验收（对应 BACKLOG P6-03「PCA 完整重建与降维误差符合定义」）：
- 3 波段合成图含精确线性相关（协方差秩 2）；
- 全成分正变换输出 3 波段 PC，sidecar 的完整重建 RMSE ≈ 0；
- 逆变换用 sidecar 重建原始三波段，逐像元比对；
- 1 个主成分时降维，sidecar 的 RMSE²×波段数 == discarded_variance（定义核对）；
- 相关矩阵模式同样可完整重建；
- NoData 像元在正/逆变换中均保持 NoData；
- 模式专属参数、越界成分数、PNG 输出等明确拒绝且不落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
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
NODATA = -9999.0


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


def _bands(rows: int, cols: int) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    band1 = 10.0 + 2.0 * x + 0.5 * y
    band2 = 20.0 - x + 0.2 * y
    band3 = 0.5 * band1 + 0.5 * band2 + 1.0  # 精确线性组合，协方差秩为 2
    return np.stack([band1, band2, band3]).astype(np.float64)


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    *,
    rows: int = 5,
    cols: int = 4,
    nodata_pixel: tuple[int, int] | None = None,
) -> Path:
    values = _bands(rows, cols)
    if nodata_pixel is not None:
        y, x = nodata_pixel
        values[:, y, x] = NODATA
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.tolist()!r}, dtype=np.float32)
rows, cols = values.shape[1], values.shape[2]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, cols, rows, values.shape[0], gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for i in range(values.shape[0]):
    band = ds.GetRasterBand(i + 1)
    band.WriteArray(values[i])
    band.SetNoDataValue({NODATA})
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
    assert proc.returncode == 0, proc.stderr
    return path


def _run(
    qapp: QApplication,
    parameters: dict[str, Any],
    source: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p603-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:pca",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="ras-in",
                kind=DatasetKind.raster,
                uri=source.resolve().as_uri(),
            )
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=target.resolve().as_uri(),
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p603-pca", project_root=str(ROOT))
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, errors
        assert results, "任务未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _inspect(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open({str(path)!r})
assert ds is not None
bands = []
for i in range(1, ds.RasterCount + 1):
    b = ds.GetRasterBand(i)
    bands.append({{
        "dtype": gdal.GetDataTypeName(b.DataType),
        "nodata": b.GetNoDataValue(),
        "description": b.GetDescription(),
        "arr": b.ReadAsArray().tolist(),
    }})
print(json.dumps({{
    "driver": ds.GetDriver().ShortName,
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "metadata": ds.GetMetadata() or {{}},
    "band_list": bands,
}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _sidecar(out: Path, suffix: str) -> dict[str, Any]:
    path = out.with_name(out.name + suffix)
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _rmse(a: np.ndarray, b: np.ndarray, valid: np.ndarray) -> float:
    diff = a[:, valid] - b[:, valid]
    return float(np.sqrt(np.mean(diff * diff)))


def test_pca_full_reconstruction_and_inverse(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 5, 4
    src = _make_raster(gdal_rt, tmp_path / "pca_src.tif", rows=rows, cols=cols)
    components = tmp_path / "pca_components.tif"

    result = _run(qapp, {"pcaMode": "forward"}, src, components)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, components)
    assert info["driver"] == "GTiff"
    assert info["bands"] == 3
    assert [b["description"] for b in info["band_list"]] == ["PC1", "PC2", "PC3"]
    assert all(b["dtype"] == "Float32" and b["nodata"] == NODATA for b in info["band_list"])

    transform = _sidecar(components, ".pca.json")
    assert transform["type"] == "spectra_pca_transform"
    assert transform["matrix"] == "covariance"
    assert transform["components"] == 3
    assert transform["band_count"] == 3
    assert len(transform["eigenvalues"]) == 3
    assert np.all(np.diff(transform["eigenvalues"]) <= 1e-12)
    assert transform["reconstruction_rmse"] <= 1e-9, "全成分重建应为浮点噪声"
    assert transform["discarded_variance"] == pytest.approx(0.0, abs=1e-9)
    assert sum(transform["explained_variance_ratio"]) == pytest.approx(1.0, rel=1e-9)

    inverse = tmp_path / "pca_inverse.tif"
    result2 = _run(
        qapp,
        {
            "pcaMode": "inverse",
            "pcaTransform": str(components.with_name(components.name + ".pca.json")),
        },
        components,
        inverse,
    )
    assert result2.status is TaskStatus.succeeded, result2.error

    original = _bands(rows, cols)
    restored = np.asarray([b["arr"] for b in _inspect(gdal_rt, inverse)["band_list"]])
    valid = np.ones(rows * cols, dtype=bool)
    assert _rmse(original.reshape(3, -1), restored.reshape(3, -1), valid) <= 1e-4

    replay = _sidecar(inverse, ".pca_inverse.json")
    assert replay["components_used"] == 3
    assert replay["band_count"] == 3
    expected_sha = hashlib.sha256(
        (components.with_name(components.name + ".pca.json")).read_bytes()
    ).hexdigest()
    assert replay["transform_sha256"] == expected_sha


def test_pca_reduced_error_matches_definition(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "pca_red_src.tif", rows=6, cols=5)
    components = tmp_path / "pca_one_component.tif"

    result = _run(qapp, {"pcaMode": "forward", "pcaComponents": 1}, src, components)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, components)
    assert info["bands"] == 1

    transform = _sidecar(components, ".pca.json")
    assert transform["components"] == 1
    assert transform["discarded_variance"] > 0.0
    assert transform["reconstruction_rmse"] > 0.0
    # 定义核对：MSE（像元×波段平均）= 丢弃特征值之和 / 波段数。
    assert transform["reconstruction_rmse"] ** 2 * transform["band_count"] == pytest.approx(
        transform["discarded_variance"], rel=1e-9
    )

    inverse = tmp_path / "pca_one_component_inverse.tif"
    result2 = _run(
        qapp,
        {
            "pcaMode": "inverse",
            "pcaComponents": 1,
            "pcaTransform": str(components.with_name(components.name + ".pca.json")),
        },
        components,
        inverse,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    original = _bands(6, 5)
    restored = np.asarray([b["arr"] for b in _inspect(gdal_rt, inverse)["band_list"]])
    measured = _rmse(original.reshape(3, -1), restored.reshape(3, -1), np.ones(30, dtype=bool))
    assert measured > 0.0
    # 逆变换读取的是 Float32 主成分，误差应与正变换报告的 float64 值同量级。
    assert measured == pytest.approx(transform["reconstruction_rmse"], rel=1e-3, abs=1e-4)


def test_pca_correlation_matrix_full_reconstruction(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "pca_corr_src.tif", rows=4, cols=4)
    components = tmp_path / "pca_corr.tif"
    result = _run(qapp, {"pcaMode": "forward", "pcaMatrix": "correlation"}, src, components)
    assert result.status is TaskStatus.succeeded, result.error
    transform = _sidecar(components, ".pca.json")
    assert transform["matrix"] == "correlation"
    assert len(transform["std"]) == 3
    assert all(value > 0 for value in transform["std"])
    assert transform["reconstruction_rmse"] <= 1e-9

    inverse = tmp_path / "pca_corr_inverse.tif"
    result2 = _run(
        qapp,
        {
            "pcaMode": "inverse",
            "pcaTransform": str(components.with_name(components.name + ".pca.json")),
        },
        components,
        inverse,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    original = _bands(4, 4)
    restored = np.asarray([b["arr"] for b in _inspect(gdal_rt, inverse)["band_list"]])
    assert _rmse(original.reshape(3, -1), restored.reshape(3, -1), np.ones(16, dtype=bool)) <= 1e-4


def test_pca_nodata_is_preserved_in_forward_and_inverse(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 5, 4
    src = _make_raster(
        gdal_rt, tmp_path / "pca_nodata_src.tif", rows=rows, cols=cols, nodata_pixel=(2, 1)
    )
    components = tmp_path / "pca_nodata_components.tif"
    result = _run(qapp, {"pcaMode": "forward"}, src, components)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, components)
    for band in info["band_list"]:
        assert band["nodata"] == NODATA
        assert band["arr"][2][1] == NODATA

    transform = _sidecar(components, ".pca.json")
    assert transform["n_valid"] == rows * cols - 1

    inverse = tmp_path / "pca_nodata_inverse.tif"
    result2 = _run(
        qapp,
        {
            "pcaMode": "inverse",
            "pcaTransform": str(components.with_name(components.name + ".pca.json")),
        },
        components,
        inverse,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    for band in _inspect(gdal_rt, inverse)["band_list"]:
        assert band["arr"][2][1] == NODATA


def test_pca_mode_and_format_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "pca_reject_src.tif", rows=4, cols=4)
    out = tmp_path / "pca_reject_out.tif"

    forward_with_transform = _run(qapp, {"pcaMode": "forward", "pcaTransform": "x.json"}, src, out)
    assert forward_with_transform.status is TaskStatus.failed
    assert forward_with_transform.error
    assert forward_with_transform.error["code"] == "invalid_parameters"
    assert not out.exists()

    inverse_without_transform = _run(qapp, {"pcaMode": "inverse"}, src, out)
    assert inverse_without_transform.status is TaskStatus.failed
    assert inverse_without_transform.error
    assert inverse_without_transform.error["code"] == "invalid_parameters"
    assert not out.exists()

    inverse_with_matrix = _run(
        qapp,
        {"pcaMode": "inverse", "pcaMatrix": "covariance", "pcaTransform": "x.json"},
        src,
        out,
    )
    assert inverse_with_matrix.status is TaskStatus.failed
    assert inverse_with_matrix.error
    assert inverse_with_matrix.error["code"] == "invalid_parameters"
    assert not out.exists()

    too_many = _run(qapp, {"pcaMode": "forward", "pcaComponents": 4}, src, out)
    assert too_many.status is TaskStatus.failed
    assert too_many.error
    assert too_many.error["code"] == "invalid_parameters"
    assert not out.exists()

    png = _run(qapp, {"pcaMode": "forward", "format": "PNG"}, src, tmp_path / "pca.png")
    assert png.status is TaskStatus.failed
    assert png.error
    assert png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "pca.png").exists()

    missing = _run(
        qapp,
        {"pcaMode": "inverse", "pcaTransform": str(tmp_path / "missing.pca.json")},
        src,
        out,
    )
    assert missing.status is TaskStatus.failed
    assert missing.error
    assert missing.error["code"] == "invalid_parameters"
    assert not out.exists()
