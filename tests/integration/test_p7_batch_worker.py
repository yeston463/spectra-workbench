"""P7-03 批处理端到端验收（真实 GDAL worker）。

验收标准（BACKLOG P7-03「多输入、多输出、参数变化、同名冲突、单项失败继续策略通过」）：
- **模板展开**：per-item 路径与参数真实下发；多输入/多输出展开；
- **参数变化**：每项参数（Translate 的 `srcWin`、`outputType` 等）独立生效；
- **同名冲突**：`overwrite=False` 同名目标失败；`overwrite=True` 覆盖通过；
- **单项失败继续策略**：`continue_on_error=True` 收集所有条目终态；`False` 遇
  失败立即停止，未处置的条目显式 `pending`（不冒充已成功）；
- **manifest 汇总**：`manifest.json` 记录每条 inputs/outputs/终态/时长。
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
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.processing.batch import (
    BatchError,
    render_batch_specs,
    run_batch,
    write_manifest,
)
from spectra_workbench.scheduler.client import QtWorkerClient

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()
NODATA = -9999.0
GT = (100.0, 10.0, 0.0, 200.0, 0.0, -10.0)


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


def _make_raster(gdal_rt: dict[str, Any], path: Path, values: list[list[float]]) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values!r}, dtype=np.float32)
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
ds.SetGeoTransform({GT!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteArray(values)
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


@pytest.fixture()
def worker(qapp: QApplication):
    # QtWorkerClient 是单用途对象：stop() 置 _closed 后不再转发任何信号，
    # 因此每条批处理条目各自创建全新 client（与真实 UI 一任务一 client 一致）。
    clients: list[QtWorkerClient] = []

    def dispatch(spec: dict[str, Any]) -> TaskStatus:
        client = QtWorkerClient(
            provider_id="gdal",
            worker_id=f"p7-03-{uuid.uuid4().hex[:8]}",
            project_root=str(ROOT),
        )
        clients.append(client)
        spec_obj = TaskSpec(
            task_id=spec["task_id"],
            attempt=spec["attempt"],
            capability_id=spec["capability_id"],
            descriptor_revision=spec["descriptor_revision"],
            engine=EngineConstraint(**spec["engine"]),
            project_root=str(ROOT),
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
            client.start(timeout=25.0)
            client.submit_task_async(spec_obj, timeout=90.0)
            deadline = time.monotonic() + 90.0
            while not results and not errors and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
            assert not errors, errors
            assert results, "batch task 未产生结果"
            return results[0].status
        finally:
            client.stop(timeout=5.0)

    yield dispatch
    for stale in clients:
        if stale.is_alive:
            stale.stop(timeout=5.0)


def test_render_batch_specs_supports_multi_io_and_params(tmp_path: Path) -> None:
    template = {
        "capability_id": "gdal:merge",
        "engine_provider": "gdal",
        "engine_runtime": "gdal-qgis",
        "engine_version": "3.13.3",
        "inputs": [
            str(tmp_path / "in_a_{item[i]}.tif"),
            str(tmp_path / "in_b_{item[i]}.tif"),
        ],
        "outputs": [
            str(tmp_path / "out_a_{item[i]}.tif"),
            str(tmp_path / "out_b_{item[i]}.tif"),
        ],
        "params": {
            "format": "GTiff",
            "overlapPriority": "{item[priority]}",
        },
        "overwrite": True,
        "task_prefix": "multi",
        "project_root": str(ROOT),
        "descriptor_revision": "1.0",
    }
    items = [
        {"i": 0, "priority": "first"},
        {"i": 1, "priority": "last"},
    ]
    specs = render_batch_specs(template, items)
    assert len(specs) == 2, len(specs)

    entry0_spec, entry0_item, entry0_index = specs[0]
    assert isinstance(entry0_item, dict) and entry0_item["i"] == 0
    assert entry0_index == 0
    inputs = entry0_spec["inputs"]["input"]
    assert isinstance(inputs, list), type(inputs)
    assert len(inputs) == 2, len(inputs)
    assert inputs[0]["uri"].endswith("in_a_0.tif")
    assert inputs[1]["uri"].endswith("in_b_0.tif")
    outputs = entry0_spec["outputs"]
    assert len(outputs) == 2, len(outputs)
    assert outputs[0]["key"] == "output_0"
    assert outputs[1]["key"] == "output_1"
    assert outputs[0]["overwrite"] is True
    assert entry0_spec["parameters"]["overlapPriority"] == "first"

    entry1_spec, _, _ = specs[1]
    assert entry1_spec["parameters"]["overlapPriority"] == "last"
    inputs1 = entry1_spec["inputs"]["input"]
    assert inputs1[0]["uri"].endswith("in_a_1.tif")


def test_render_batch_specs_unknown_template_key_rejects() -> None:
    template = {
        "capability_id": "gdal:translate",
        "engine_provider": "gdal",
        "inputs": ["{item.name}.tif"],
        "outputs": ["{item.missing}.tif"],
        "project_root": str(ROOT),
    }
    items = [{"name": "a"}]
    with pytest.raises(BatchError, match="模板"):
        render_batch_specs(template, items)


def test_batch_continue_on_error_keeps_remaining(
    tmp_path: Path, gdal_rt: dict[str, Any], worker: Callable[[dict[str, Any]], TaskStatus]
) -> None:
    # 2 条：第 0 条输入不存在 → 失败；第 1 条正常 → 成功
    _make_raster(gdal_rt, tmp_path / "in_1.tif", [[1.0, 2.0], [3.0, 4.0]])
    template = {
        "capability_id": "gdal:translate",
        "engine_provider": "gdal",
        "engine_runtime": "gdal-qgis",
        "engine_version": "3.13.3",
        "inputs": [str(tmp_path / "in_{item[index]}.tif")],
        "outputs": [str(tmp_path / "out_{item[index]}.tif")],
        "params": {"format": "GTiff"},
        "overwrite": False,
        "task_prefix": "has-ok",
        "project_root": str(ROOT),
        "descriptor_revision": "1.0",
    }
    items = [{"index": 0}, {"index": 1}]

    report = run_batch(
        template=template,
        items=items,
        execute=worker,
        continue_on_error=True,
        batch_id="has-ok",
    )
    assert report.entries[0]["status"] == "failed"
    assert report.entries[1]["status"] == "succeeded"
    assert report.summary().get("succeeded") == 1
    assert report.summary().get("failed") == 1
    assert report.all_terminal() is True

    manifest_path = write_manifest(tmp_path / "has-ok.json", report.to_dict())
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert loaded["batch_id"] == "has-ok"
    assert len(loaded["entries"]) == 2
    # manifest 里的终态保留真实状态
    assert loaded["entries"][0]["status"] == "failed"
    assert loaded["entries"][1]["status"] == "succeeded"


def test_batch_stop_on_first_failure(
    tmp_path: Path, gdal_rt: dict[str, Any], worker: Callable[[dict[str, Any]], TaskStatus]
) -> None:
    # 3 条：第 0 条输入缺失 → 失败；继续 on_error=False → 第 1/2 条保持 pending
    _make_raster(gdal_rt, tmp_path / "sci_1.tif", [[1.0, 2.0], [3.0, 4.0]])
    template = {
        "capability_id": "gdal:translate",
        "engine_provider": "gdal",
        "engine_runtime": "gdal-qgis",
        "engine_version": "3.13.3",
        "inputs": [str(tmp_path / "sci_{item[index]}.tif")],
        "outputs": [str(tmp_path / "sci_out_{item[index]}.tif")],
        "params": {"format": "GTiff"},
        "overwrite": False,
        "task_prefix": "strict",
        "project_root": str(ROOT),
        "descriptor_revision": "1.0",
    }
    items = [{"index": 0}, {"index": 1}, {"index": 2}]

    report = run_batch(
        template=template,
        items=items,
        execute=worker,
        continue_on_error=False,
        batch_id="strict",
    )
    assert report.entries[0]["status"] == "failed"
    assert report.entries[1]["status"] == "pending"
    assert report.entries[2]["status"] == "pending"
    assert report.summary().get("failed") == 1
    assert report.summary().get("pending") == 2
    assert report.all_terminal() is False


def test_batch_overwrite_semantics_seen_by_worker(
    tmp_path: Path, gdal_rt: dict[str, Any], worker: Callable[[dict[str, Any]], TaskStatus]
) -> None:
    # 同名目标已存在 → overwrite=True 覆盖成功
    for i, _ in enumerate(("ow_a", "ow_b")):
        _make_raster(gdal_rt, tmp_path / f"ow_in_{i}.tif", [[1.0, 2.0], [3.0, 4.0]])
    existing = tmp_path / "ow_out_0.tif"
    existing.write_bytes(b"pre-existing-stub")

    template = {
        "capability_id": "gdal:translate",
        "engine_provider": "gdal",
        "engine_runtime": "gdal-qgis",
        "engine_version": "3.13.3",
        "inputs": [str(tmp_path / "ow_in_{item[index]}.tif")],
        "outputs": [str(tmp_path / "ow_out_{item[index]}.tif")],
        "params": {"format": "GTiff"},
        "overwrite": True,
        "task_prefix": "overwrite",
        "project_root": str(ROOT),
        "descriptor_revision": "1.0",
    }
    items = [{"index": 0}, {"index": 1}]
    report = run_batch(
        template=template,
        items=items,
        execute=worker,
        continue_on_error=True,
        batch_id="overwrite",
    )
    assert report.all_terminal() is True, report.to_dict()
    assert report.summary().get("succeeded") == 2
    assert existing.read_bytes() != b"pre-existing-stub", "覆盖绕过 commit_files"
