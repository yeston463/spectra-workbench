"""P6-04 GCP 配准端到端：合成已知变换 + 独立检查点 + 参考网格重采样。

验收（对应 BACKLOG P6-04）：
- 仿射合成：控制点/检查点都由已知仿射生成，affine 与 polynomial2 均应
  恢复该仿射；检查点 RMSE 近零；输出与参考影像的解析平面一致；
- 二次多项式合成：affine 的独立检查点误差显著大于 polynomial2；
  polynomial2 输出与参考影像近似一致；
- GCP 文件/变换/格式拒绝路径明确且不落盘；NoData 保持。
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
REF_GT = (0.0, 1.0, 0.0, 300.0, 0.0, -1.0)
REF_W = 20
REF_H = 20


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


def _plane(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return 10.0 + 0.1 * x + 0.2 * y


def _write_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    gt: tuple[float, ...],
    values: np.ndarray | None,
    *,
    rows: int,
    cols: int,
    mapping: Any = None,
) -> Path:
    if values is None:
        arr = np.empty((rows, cols), dtype=np.float64)
        for py in range(rows):
            for px in range(cols):
                x, y = mapping(px + 0.5, py + 0.5)
                arr[py, px] = _plane(np.array(x), np.array(y))
    else:
        arr = np.asarray(values, dtype=np.float64)
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({arr.tolist()!r}, dtype=np.float32)
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
ds.GetRasterBand(1).WriteArray(values)
ds.GetRasterBand(1).SetNoDataValue({NODATA})
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


def _make_reference(gdal_rt: dict[str, Any], path: Path) -> Path:
    return _write_raster(
        gdal_rt, path, REF_GT, None, rows=REF_H, cols=REF_W,
        mapping=lambda px, py: (
            REF_GT[0] + REF_GT[1] * px + REF_GT[2] * py,
            REF_GT[3] + REF_GT[4] * px + REF_GT[5] * py,
        ),
    )


def _write_gcps(path: Path, mapping: Any, *, rows: int, cols: int, step: int = 7) -> Path:
    points = []
    index = 0
    for py in range(1, rows, step):
        for px in range(1, cols, step):
            x, y = mapping(px + 0.5, py + 0.5)
            role = "control" if index % 3 != 2 else "check"
            points.append(
                {
                    "id": f"p{index}",
                    "pixel": px + 0.5,
                    "line": py + 0.5,
                    "x": float(x),
                    "y": float(y),
                    "role": role,
                }
            )
            index += 1
    payload = {
        "type": "spectra_gcp_set",
        "schema_version": "1.0",
        "srs": "EPSG:32650",
        "points": points,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(
    qapp: QApplication,
    parameters: dict[str, Any],
    source: Path,
    target: Path,
    *,
    timeout: float = 90.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p604gcp-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:gcpregister",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p604-gcp", project_root=str(ROOT))
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
b = ds.GetRasterBand(1)
print(json.dumps({{
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "gt": ds.GetGeoTransform(),
    "metadata": ds.GetMetadata() or {{}},
    "arr": b.ReadAsArray().tolist(),
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


def _sidecar(out: Path) -> dict[str, Any]:
    path = out.with_name(out.name + ".gcpregistration.json")
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_affine_gcp_registration_recovers_reference_grid(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    ref = _make_reference(gdal_rt, tmp_path / "ref.tif")
    src_gt = (-20.0, 1.0, 0.05, 320.0, 0.02, -1.0)
    source = _write_raster(
        gdal_rt,
        tmp_path / "src.tif",
        src_gt,
        None,
        rows=45,
        cols=45,
        mapping=lambda px, py: (
            src_gt[0] + src_gt[1] * px + src_gt[2] * py,
            src_gt[3] + src_gt[4] * px + src_gt[5] * py,
        ),
    )
    gcp = _write_gcps(tmp_path / "gcps.json", lambda px, py: (
        src_gt[0] + src_gt[1] * px + src_gt[2] * py,
        src_gt[3] + src_gt[4] * px + src_gt[5] * py,
    ), rows=45, cols=45)

    for transform_name in ("affine", "polynomial2"):
        out = tmp_path / f"registered_{transform_name}.tif"
        result = _run(
            qapp,
            {
                "gcpFile": str(gcp),
                "referenceFile": str(ref),
                "gcpTransform": transform_name,
                "gcpResample": "bilinear",
            },
            source,
            out,
        )
        assert result.status is TaskStatus.succeeded, result.error
        info = _inspect(gdal_rt, out)
        assert info["w"] == REF_W and info["h"] == REF_H
        assert info["gt"] == pytest.approx(list(REF_GT))

        # 参考影像的解析平面（像素中心地图坐标）
        expected = np.array(
            [
                [
                    _plane(
                        np.array(REF_GT[0] + REF_GT[1] * (x + 0.5)),
                        np.array(REF_GT[3] + REF_GT[5] * (y + 0.5)),
                    )
                    for x in range(REF_W)
                ]
                for y in range(REF_H)
            ]
        )
        actual = np.asarray(info["arr"], dtype=np.float64)
        assert float(np.max(np.abs(actual - expected))) <= 1e-3

        payload = _sidecar(out)
        assert payload["transform"] == transform_name
        assert payload["control_points"] > 0
        assert payload["check_points"] > 0
        assert payload["forward_checkpoint_error"]["rmse"] <= 1e-8
        assert payload["backward_checkpoint_error"]["rmse"] <= 1e-8
        assert payload["output_grid"]["width"] == REF_W
        assert payload["output_grid"]["height"] == REF_H


def test_quadratic_gcp_requires_polynomial2_and_checkpoints_separate(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    ref = _make_reference(gdal_rt, tmp_path / "ref_quad.tif")

    def quad(px: float, py: float) -> tuple[float, float]:
        return (
            -5.0 + 1.0 * px + 0.02 * py + 0.003 * px * px,
            305.0 + 0.01 * px - 1.0 * py - 0.003 * py * py,
        )

    source = _write_raster(
        gdal_rt,
        tmp_path / "src_quad.tif",
        (0.0, 1.0, 0.0, 0.0, 0.0, -1.0),
        None,
        rows=40,
        cols=40,
        mapping=quad,
    )
    gcp = _write_gcps(tmp_path / "gcps_quad.json", quad, rows=40, cols=40, step=6)

    affine_out = tmp_path / "quad_affine.tif"
    affine_result = _run(
        qapp,
        {
            "gcpFile": str(gcp),
            "referenceFile": str(ref),
            "gcpTransform": "affine",
            "gcpResample": "bilinear",
        },
        source,
        affine_out,
    )
    assert affine_result.status is TaskStatus.succeeded, affine_result.error
    affine_payload = _sidecar(affine_out)
    assert affine_payload["forward_checkpoint_error"]["rmse"] > 0.1

    poly_out = tmp_path / "quad_poly.tif"
    poly_result = _run(
        qapp,
        {
            "gcpFile": str(gcp),
            "referenceFile": str(ref),
            "gcpTransform": "polynomial2",
            "gcpResample": "bilinear",
        },
        source,
        poly_out,
    )
    assert poly_result.status is TaskStatus.succeeded, poly_result.error
    poly_payload = _sidecar(poly_out)
    assert poly_payload["forward_checkpoint_error"]["rmse"] <= 1e-8
    assert poly_payload["forward_checkpoint_error"]["rmse"] < affine_payload[
        "forward_checkpoint_error"
    ]["rmse"]

    expected = np.array(
        [
            [
                _plane(
                    np.array(REF_GT[0] + REF_GT[1] * (x + 0.5)),
                    np.array(REF_GT[3] + REF_GT[5] * (y + 0.5)),
                )
                for x in range(REF_W)
            ]
            for y in range(REF_H)
        ]
    )
    actual = np.asarray(_inspect(gdal_rt, poly_out)["arr"], dtype=np.float64)
    # 反二次多项式是近似逆，允许亚像元级插值误差。
    assert float(np.max(np.abs(actual - expected))) <= 0.5


def test_gcp_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    ref = _make_reference(gdal_rt, tmp_path / "ref_rej.tif")
    src_gt = (-20.0, 1.0, 0.05, 320.0, 0.02, -1.0)
    source = _write_raster(
        gdal_rt, tmp_path / "src_rej.tif", src_gt, None, rows=45, cols=45,
        mapping=lambda px, py: (src_gt[0] + px, src_gt[3] - py),
    )
    good_gcp = _write_gcps(
        tmp_path / "gcps_rej.json",
        lambda px, py: (src_gt[0] + px, src_gt[3] - py),
        rows=45,
        cols=45,
    )
    out = tmp_path / "reject_out.tif"

    missing = _run(qapp, {"referenceFile": str(ref)}, source, out)
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert not out.exists()

    bad_transform = _run(
        qapp,
        {"gcpFile": str(good_gcp), "referenceFile": str(ref), "gcpTransform": "tps"},
        source,
        out,
    )
    assert bad_transform.status is TaskStatus.failed
    assert bad_transform.error and bad_transform.error["code"] == "invalid_parameters"
    assert not out.exists()

    png = _run(
        qapp,
        {
            "gcpFile": str(good_gcp),
            "referenceFile": str(ref),
            "gcpTransform": "affine",
            "format": "PNG",
        },
        source,
        tmp_path / "gcp.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "gcp.png").exists()
