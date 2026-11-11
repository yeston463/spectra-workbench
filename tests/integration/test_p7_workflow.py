"""P7-04 DAG 工作流端到端验收（真实 GDAL/Rasterio worker）。

验收标准（BACKLOG P7-04「环/端口错配拒绝；失败、恢复、缓存失效和跨引擎
物化通过」，对齐计划 §11）：
- **提交前校验**：环检测、端口类型错配、未知上游引用、输出 URI 冲突与
  「写穿他人输入源」在执行前显式拒绝；
- **跨引擎物化**：gdal 节点产物作为真实文件衔接 rasterio 节点
  （TaskSpec 通道，两个 provider 全部真进程通信）；
- **失败/恢复**：节点失败 → 下游显式 pending（skipped），独立分支继续；
  输入/参数/引擎 node_key 与输出指纹未变的成功节点重跑以 `cached`
  复用（引擎调用数为 0 —— 未执行的直接证据）；
- **缓存失效**：上游参数变化 → 上游重跑 → 下游缓存自动失效重执行，
  新产物内容真实进入下游（数值验证）；
- manifest 记录指纹复现等级（fast/strong）。
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
from spectra_workbench.processing.workflow import (
    WorkflowValidationError,
    render_workflow_nodes,
    run_workflow,
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
    """QGIS 引擎 python 建 2×2 合成 GTiff（GUI 进程零原生依赖）。"""
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
    """QGIS 引擎 python 独立重开并转储 JSON：尺寸/GT/dtype/像元值。"""
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
  "nodata": band.GetNoDataValue(),
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
    """逐节点分发器：按 spec 引擎 provider 逐次创建全新 QtWorkerClient。

    `calls["wf-<node>]` 只在真实执行时递增 —— cached 节点绝对不会出现，
    是「未执行」的直接证据；测试里比较**每次运行前的快照差值**。
    """
    calls: dict[str, int] = {}
    clients: list[QtWorkerClient] = []

    from spectra_workbench.domain.models import (
        DatasetRef,
        EngineConstraint,
        OutputPlan,
        TaskSpec,
    )

    def _dispatch(spec: dict[str, Any]) -> Any:
        provider = spec["engine"]["provider_id"]
        client = QtWorkerClient(
            provider_id=provider,
            worker_id=f"wf-{uuid.uuid4().hex[:8]}",
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
            assert results, "工作流节点未产生结果"
            node_key = "-".join(spec["task_id"].split("-")[:2])  # "wf-<node>"
            calls[node_key] = calls.get(node_key, 0) + 1
            return results[0]  # TaskResult（status + error 详情都进报告）
        finally:
            client.stop(timeout=5.0)

    _dispatch.calls = calls  # type: ignore[attr-defined]

    yield _dispatch

    for stale in clients:
        if stale.is_alive:
            stale.stop(timeout=5.0)


def _doc(nodes: list[dict[str, Any]], project_root: Path) -> dict[str, Any]:
    return {"schema_version": "1.0", "project_root": str(project_root), "nodes": nodes}


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: v - before.get(k, 0) for k, v in after.items() if v != before.get(k, 0)}


# ============================================================ 1. 提交前校验
def test_workflow_prevalidation_rejects_cycle_port_and_conflicts(tmp_path: Path) -> None:
    src = tmp_path / "x.tif"  # 校验阶段不要求文件存在

    # 环：A → B → A
    with pytest.raises(WorkflowValidationError, match="环"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "a",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["node", "b", "output"]},
                        "outputs": {"output": str(tmp_path / "a_out.tif")},
                    },
                    {
                        "id": "b",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["node", "a", "output"]},
                        "outputs": {"output": str(tmp_path / "b_out.tif")},
                    },
                ],
                tmp_path,
            )
        )

    # 端口错配：上游声明 vector，下游缺省期望 raster
    with pytest.raises(WorkflowValidationError, match="端口错配"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "v",
                        "capability_id": "gdal:ogr_translate",
                        "inputs": {"input": ["literal", str(src), "vector"]},
                        "outputs": {
                            "output": {
                                "uri": str(tmp_path / "v_out.gpkg"),
                                "kind": "vector",
                            }
                        },
                    },
                    {
                        "id": "r",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["node", "v", "output"]},
                        "outputs": {"output": str(tmp_path / "r_out.tif")},
                    },
                ],
                tmp_path,
            )
        )

    # 未知上游节点引用
    with pytest.raises(WorkflowValidationError, match="不存在的上游节点"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "c",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["node", "ghost", "output"]},
                        "outputs": {"output": str(tmp_path / "c_out.tif")},
                    }
                ],
                tmp_path,
            )
        )

    # 上游不存在的输出键
    with pytest.raises(WorkflowValidationError, match="不存在的输出"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "u",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["literal", str(src)]},
                        "outputs": {"output": str(tmp_path / "u_out.tif")},
                    },
                    {
                        "id": "d",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["node", "u", "wrong_key"]},
                        "outputs": {"output": str(tmp_path / "d_out.tif")},
                    },
                ],
                tmp_path,
            )
        )

    good = str(tmp_path / "shared.tif")

    # 输出 URI 冲突
    with pytest.raises(WorkflowValidationError, match="产出冲突"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "d1",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["literal", str(src)]},
                        "outputs": {"output": good},
                    },
                    {
                        "id": "d2",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["literal", str(src)]},
                        "outputs": {"output": good},
                    },
                ],
                tmp_path,
            )
        )

    # 输出 URI 写穿他人输入源
    with pytest.raises(WorkflowValidationError, match="写穿冲突"):
        render_workflow_nodes(
            _doc(
                [
                    {
                        "id": "w",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["literal", good]},
                        "outputs": {"output": str(tmp_path / "w_out.tif")},
                    },
                    {
                        "id": "z",
                        "capability_id": "gdal:translate",
                        "inputs": {"input": ["literal", str(src)]},
                        "outputs": {"output": good},
                    },
                ],
                tmp_path,
            )
        )


# ==================================================== 2. 跨引擎物化 + 缓存
def test_workflow_cross_engine_materialization_and_reuse(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "src.tif", [[1.0, 2.0], [3.0, 4.0]])
    doc = _doc(
        [
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
        tmp_path,
    )

    report = run_workflow(
        doc,
        execute=dispatch,
        workflow_id="wf-cross",
        cache_dir=tmp_path,
        continue_on_error=True,
    )
    assert report.entries["n_gdal"]["status"] == "succeeded"
    assert report.entries["n_rio"]["status"] == "succeeded"
    assert report.all_terminal() is True
    assert (tmp_path / "mid.tif").is_file()
    final = tmp_path / "final.tif"
    assert final.is_file()

    # 数值验证：rasterio 物化与 gdal 上游同尺寸/同 GT/同像元（引擎独立重开）
    dump = _dump_gtiff(gdal_rt, final)
    assert dump["width"] == 2 and dump["height"] == 2
    assert dump["values"] == [[1.0, 2.0], [3.0, 4.0]]
    assert dump["geotransform"][0] == GT[0]

    # 恢复：无变化重跑 → 双节点 cached，引擎调用数 0
    before = dict(dispatch.calls)  # type: ignore[attr-defined]
    report2 = run_workflow(
        doc,
        execute=dispatch,
        workflow_id="wf-cross",
        cache_dir=tmp_path,
        continue_on_error=True,
    )
    delta = _delta(before, dispatch.calls)  # type: ignore[attr-defined]
    assert delta == {}, f"cached 不得执行引擎，实际 {delta}"
    assert report2.summary().get("cached") == 2
    assert report2.entries["n_rio"]["cache"]["hit"] is True


# ============================================================ 3. 失败/恢复
def _chain_doc(
    tmp_path: Path, src: Path, missing: Path, *, n_fail_extra_params: dict | None = None
) -> dict[str, Any]:
    fail_params: dict[str, Any] = {"format": "GTiff"}
    if n_fail_extra_params:
        fail_params.update(n_fail_extra_params)
    return _doc(
        [
            {
                "id": "n_src",
                "capability_id": "gdal:translate",
                "inputs": {"input": ["literal", str(src)]},
                "outputs": {"output": str(tmp_path / "midA.tif")},
                "params": {"format": "GTiff"},
            },
            {
                "id": "n_fail",
                "capability_id": "gdal:translate",
                "inputs": {"input": ["literal", str(missing)]},
                "outputs": {"output": str(tmp_path / "midB.tif")},
                "params": fail_params,
            },
            {
                "id": "n_ds",
                "capability_id": "rasterio:convert",
                "engine_provider": "rasterio",
                "inputs": {"input": ["node", "n_fail", "output"]},
                "outputs": {"output": str(tmp_path / "dsB.tif")},
                "params": {"format": "GTiff"},
                "driver": "GTiff",
            },
            {
                "id": "n_ind",
                "capability_id": "gdal:translate",
                "inputs": {"input": ["literal", str(src)]},
                "outputs": {"output": str(tmp_path / "ind.tif")},
                "params": {"format": "GTiff"},
            },
        ],
        tmp_path,
    )


def test_workflow_failure_recovery_and_cache_invalidation(
    tmp_path: Path, gdal_rt: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any]
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "src.tif", [[1.0, 2.0], [3.0, 4.0]])
    # 损坏输入：存在但不是合法 GTiff → 引擎级真实失败（非预检失败）
    corrupt = tmp_path / "corrupt.tif"
    corrupt.write_bytes(b"junk-not-a-gtiff")

    # ---- run 1：n_fail 引擎失败 → n_ds pending(skipped)；独立分支 n_ind 成功 ----
    doc = _chain_doc(tmp_path, src, corrupt)
    before = dict(dispatch.calls)  # type: ignore[attr-defined]
    report = run_workflow(
        doc,
        execute=dispatch,
        workflow_id="wf-recover",
        cache_dir=tmp_path,
        continue_on_error=True,
    )
    delta = _delta(before, dispatch.calls)  # type: ignore[attr-defined]
    assert set(delta) == {"wf-n_src", "wf-n_fail", "wf-n_ind"}, delta
    assert report.entries["n_src"]["status"] == "succeeded"
    assert report.entries["n_fail"]["status"] == "failed"
    assert report.entries["n_ds"]["status"] == "pending"
    assert report.entries["n_ds"]["error"]["code"] == "skipped"
    assert report.entries["n_ind"]["status"] == "succeeded"
    assert report.summary().get("failed") == 1
    assert report.summary().get("pending") == 1

    # ---- 修复损坏输入后 run 2：未变分支 cached（不重跑、不重写文件）----
    corrupt.unlink(missing_ok=True)
    _make_raster(gdal_rt, corrupt, [[9.0, 8.0], [7.0, 6.0]])
    ind_mtime = (tmp_path / "ind.tif").stat().st_mtime_ns
    before = dict(dispatch.calls)  # type: ignore[attr-defined]
    report2 = run_workflow(
        doc,
        execute=dispatch,
        workflow_id="wf-recover",
        cache_dir=tmp_path,
        continue_on_error=True,
    )
    delta = _delta(before, dispatch.calls)  # type: ignore[attr-defined]
    assert set(delta) == {"wf-n_fail", "wf-n_ds"}, f"未变分支不得重跑，实际 {delta}"
    assert report2.entries["n_src"]["status"] == "cached"
    assert report2.entries["n_ind"]["status"] == "cached"
    assert (tmp_path / "ind.tif").stat().st_mtime_ns == ind_mtime
    assert report2.entries["n_fail"]["status"] == "succeeded"
    assert report2.entries["n_ds"]["status"] == "succeeded"
    ds_dump = _dump_gtiff(gdal_rt, tmp_path / "dsB.tif")
    assert ds_dump["values"] == [[9.0, 8.0], [7.0, 6.0]], "下游必须消费重跑后的真实产物"
    assert report2.summary().get("cached") == 2

    # ---- 缓存失效：上游 n_fail 参数真实变化（outputType Byte）----
    doc3 = _chain_doc(tmp_path, src, corrupt, n_fail_extra_params={"outputType": "Byte"})
    before = dict(dispatch.calls)  # type: ignore[attr-defined]
    report3 = run_workflow(
        doc3,
        execute=dispatch,
        workflow_id="wf-recover",
        cache_dir=tmp_path,
        continue_on_error=True,
    )
    delta = _delta(before, dispatch.calls)  # type: ignore[attr-defined]
    assert set(delta) == {"wf-n_fail", "wf-n_ds"}, f"参数变化应仅重跑失效链，实际 {delta}"
    assert report3.entries["n_src"]["status"] == "cached"
    assert report3.entries["n_ind"]["status"] == "cached"
    assert report3.entries["n_fail"]["status"] == "succeeded"
    assert report3.entries["n_ds"]["status"] == "succeeded"
    mid_dump = _dump_gtiff(gdal_rt, tmp_path / "midB.tif")
    assert mid_dump["dtype"] == "int8" or mid_dump["dtype"].startswith("ui"), (
        f"上游 outputType=Byte 后产物应为整型，实得 {mid_dump['dtype']}"
    )
    ds_dump3 = _dump_gtiff(gdal_rt, tmp_path / "dsB.tif")
    assert ds_dump3["values"] == [[9.0, 8.0], [7.0, 6.0]], "下游必须消费失效后的新产物"
