"""P7-05 历史与可复现导出端到端验收（真实 GDAL/Rasterio worker）。

验收标准（BACKLOG P7-05「从历史重开参数；记录输入/版本/关键配置；导出
的执行入口跑出同类成果」）：
- **记录**：输入（URI/kind + 指纹）、版本（spec 引擎约束 + worker 上报
  真实版本）、关键配置（descriptor_revision/完整参数/状态/时长/错误）
  入 SQLite（schema_version 显式迁移）；
- **从历史重开参数**：reopen_params 还原可再次提交请求，重提后跑出同类
  数值成果；
- **导出执行入口**：export_workflow_recipe 生成标准 workflow 文档（边
  自动合成），P7-04 run_workflow 直接回放，跨引擎链数值与历史一致。
"""

from __future__ import annotations

import json
import os
import sqlite3
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
from spectra_workbench.processing.history import HistoryError, HistoryStore
from spectra_workbench.processing.workflow import run_workflow
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
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
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


def _dump_gtiff(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
import numpy as np
from osgeo import gdal
src = gdal.Open({str(path)!r}, gdal.GA_ReadOnly)
band = src.GetRasterBand(1)
arr = band.ReadAsArray()
print(json.dumps({{
  "width": src.RasterXSize, "height": src.RasterYSize,
  "geotransform": src.GetGeoTransform(),
  "dtype": str(arr.dtype),
  "values": arr.tolist(),
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
    return json.loads(proc.stdout)


@pytest.fixture()
def dispatch(qapp: QApplication):
    """逐节点真实分发（该 TaskResult 终态交由测试显式 record()）。"""
    clients: list[QtWorkerClient] = []

    def _dispatch(spec: dict[str, Any]) -> Any:
        provider = spec["engine"]["provider_id"]
        client = QtWorkerClient(
            provider_id=provider,
            worker_id=f"hist-{uuid.uuid4().hex[:8]}",
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


def _make_spec_dict(
    *,
    task_id: str,
    capability_id: str,
    provider: str,
    in_path: Path,
    out_path: Path,
    params: dict[str, Any] | None = None,
    driver: str | None = None,
) -> dict[str, Any]:
    rt_default, ver_default = (
        ("gdal-qgis", "3.13.3") if provider == "gdal" else ("rasterio-venv", "1.5.2")
    )
    spec = TaskSpec.model_validate(
        {
            "task_id": task_id,
            "attempt": 1,
            "capability_id": capability_id,
            "descriptor_revision": "1.0",
            "engine": EngineConstraint(
                provider_id=provider,
                runtime_id=rt_default,
                expected_version=ver_default,
            ).model_dump(mode="json"),
            "project_root": str(ROOT),
            "workspace": str(out_path.parent),
            "inputs": {
                "input": DatasetRef(
                    dataset_id="hist-in",
                    kind=DatasetKind.raster,
                    uri=in_path.resolve().as_uri(),
                ).model_dump(mode="json")
            },
            "outputs": [
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_path.resolve().as_uri(),
                    driver=driver,
                    overwrite=True,
                ).model_dump(mode="json")
            ],
            "parameters": params if params is not None else {"format": "GTiff"},
        }
    )
    return spec.model_dump(mode="json")


# ===================================================== 1. 记录 + 参数重开重提
def test_history_records_inputs_versions_and_reopen_params(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "inA.tif", [[1.0, 2.0], [3.0, 4.0]])
    out = tmp_path / "outA.tif"
    spec = _make_spec_dict(
        task_id="hist-task-1",
        capability_id="gdal:translate",
        provider="gdal",
        in_path=src,
        out_path=out,
    )
    result = dispatch(spec)

    store = HistoryStore(tmp_path / "history.sqlite")
    run_id = store.record(spec, result, kind="task", strong=True)
    assert run_id.startswith("run-")

    rec = store.get(run_id)
    assert rec["kind"] == "task"
    assert rec["status"] == "succeeded"
    assert rec["capability_id"] == "gdal:translate"
    # worker 上报的真实引擎版本（非 spec 声明值）
    assert rec["engine"]["actual_version"], rec
    assert rec["engine"]["actual_version"].startswith("3.")
    assert rec["engine"]["provider_id"] == "gdal"
    # 输入指纹（ stronG 含 SHA-256）
    assert rec["inputs"], rec
    ip = rec["inputs"][0]
    assert ip["uri"].endswith("inA.tif")
    assert ip["sha256"], ip
    assert ip["size"] > 0
    # 输出登记 committed 产物
    assert any(o["uri"].endswith("outA.tif") for o in rec["outputs"])
    assert rec["params"] == {"format": "GTiff"}
    assert rec["descriptor_revision"] == "1.0"
    assert rec["duration_seconds"] and rec["duration_seconds"] >= 0

    # delete from history == 重新提交入口
    reopened = store.reopen_params(run_id)
    assert reopened["capability_id"] == "gdal:translate"
    assert reopened["params"] == {"format": "GTiff"}
    assert reopened["engine"]["provider_id"] == "gdal"
    assert reopened["inputs"]["input"]["uri"] == ip["uri"]

    # 重提 = "同类成果"：同参数/同输入/新输出 → 数值与历史一致
    out2 = tmp_path / "outA_replay.tif"
    spec2 = _make_spec_dict(
        task_id="hist-task-2",
        capability_id=reopened["capability_id"],
        provider=reopened["engine"]["provider_id"],
        in_path=Path(_uri_path(reopened["inputs"]["input"]["uri"])),
        out_path=out2,
        params=reopened["params"],
    )
    result2 = dispatch(spec2)
    assert result2.status.value == "succeeded"
    d1 = _dump_gtiff(gdal_rt, out)
    d2 = _dump_gtiff(gdal_rt, out2)
    assert d2["values"] == d1["values"] == [[1.0, 2.0], [3.0, 4.0]]
    assert d2["width"] == 2 and d2["height"] == 2


def _uri_path(uri: str) -> str:
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    return url2pathname(urlparse(uri).path)


# ============================================ 2. 组记录 + 导出配方回放（跨引擎）
def test_history_export_recipe_replays_cross_engine_workflow(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    from typing import Any as _Any

    src = _make_raster(gdal_rt, tmp_path / "src.tif", [[5.0, 6.0], [7.0, 8.0]])
    store = HistoryStore(tmp_path / "history.sqlite")

    # ---- 记录包装：每个真实节点执行后立即 record（kind=workflow_node, 组=wf）----
    calls: dict[str, int] = {}
    recorded: list[str] = []

    def execute(spec: dict[str, Any]) -> _Any:
        result = dispatch(spec)
        cap = spec["capability_id"]
        calls[cap] = calls.get(cap, 0) + 1
        recorded.append(store.record(spec, result, kind="workflow_node", group_id="wf-demo"))
        return result

    calls: dict[str, int] = {}
    recorded: list[str] = []

    doc = {
        "schema_version": "1.0",
        "project_root": str(ROOT),
        "nodes": [
            {
                "id": "n_gdal",
                "capability_id": "gdal:translate",
                "engine_provider": "gdal",
                "inputs": {"input": ["literal", str(src)]},
                "outputs": {"output": str(tmp_path / "mid.tif")},
                "params": {"format": "GTiff"},
            },
            {
                "id": "n_rio",
                "capability_id": "rasterio:convert",
                "engine_provider": "rasterio",
                "inputs": {"input": ["node", "n_gdal", "output"]},
                "outputs": {"output": str(tmp_path / "final.tif")},
                "params": {"format": "GTiff"},
                "driver": "GTiff",
            },
        ],
    }
    report = run_workflow(
        doc,
        execute=execute,
        workflow_id="wf-demo",
        cache_dir=tmp_path / "cache",
    )
    assert report.summary().get("succeeded") == 2
    assert len(recorded) == 2
    assert {r["run_id"] for r in store.runs_by_group("wf-demo")} == set(recorded)

    # ---- 导出配方（标准 workflow 文档，边自动合成）----
    recipe_path = tmp_path / "replay_workflow.json"
    recipe = store.export_workflow_recipe(
        [r["run_id"] for r in store.runs_by_group("wf-demo")],
        recipe_path,
    )
    assert recipe_path.is_file()
    nodes = recipe["nodes"]
    assert len(nodes) == 2
    rio_node = next(n for n in nodes if "rasterio" in n["capability_id"])
    gdal_node = next(n for n in nodes if n["capability_id"] == "gdal:translate")
    # 边合成：rasterio 节点消费 gdal 输出 → node 引用
    assert rio_node["inputs"]["input"][0] == "node"
    assert rio_node["inputs"]["input"][1] == gdal_node["id"]
    # 参数/引擎原样回到文档
    assert gdal_node["params"] == {"format": "GTiff"}
    assert gdal_node["engine_provider"] == "gdal"

    # ---- 用 P7-04 run_workflow 直接回放配方：跑出同类成果 ----
    replay_cache = tmp_path / "replay_cache"
    mid_mtime = (tmp_path / "mid.tif").stat().st_mtime_ns
    before_calls = dict(calls)
    replay = run_workflow(
        recipe,
        execute=execute,
        workflow_id="wf-demo",
        cache_dir=replay_cache,
    )
    assert len(replay.order) == 2
    for entry in replay.entries.values():
        assert entry["status"] == "succeeded", json.dumps(entry, ensure_ascii=False)
    # 回放是真实执行（缓存目录全新 → 每个能力引擎恰好再次调用一次）
    replay_calls = {
        cap: n - before_calls.get(cap, 0)
        for cap, n in calls.items()
        if n != before_calls.get(cap, 0)
    }
    assert replay_calls == {"gdal:translate": 1, "rasterio:convert": 1}, replay_calls
    # 数值同类：最终产物与历史一致；中间产物被回放真实重写（mtime 变化）
    d_hist = _dump_gtiff(gdal_rt, tmp_path / "final.tif")
    assert d_hist["values"] == [[5.0, 6.0], [7.0, 8.0]]
    assert (tmp_path / "mid.tif").stat().st_mtime_ns != mid_mtime


# ================================================ 3. schema 迁移与版本守门
def test_history_schema_migration_and_newer_version_refused(tmp_path: Path) -> None:
    db = tmp_path / "history.sqlite"

    store = HistoryStore(db)
    assert store.schema_version() == 1
    conn = sqlite3.connect(db)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"meta", "runs", "run_inputs", "run_outputs"} <= tables
    # 人为降级：模拟旧库 → 重新打开应自动迁移回 1
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    store2 = HistoryStore(db)
    assert store2.schema_version() == 1
    store2.close()

    # 更高未知版本 → 拒绝打开（绝不静默降级）
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(HistoryError, match="高于当前支持"):
        HistoryStore(db)
