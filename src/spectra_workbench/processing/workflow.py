"""P7-04 DAG 工作流：带版本的节点图 + 提交前校验 + 串行执行 + 物化缓存。

语义（对齐 `docs/plan/01_完整开发方案.md` §11）：
- 工作流为**带版本的 DAG**，节点引用稳定 ``capability_id``、引擎约束、
  参数与**类型化端口**；提交前做环检测、参数与端口检查、路径冲突与
  资源汇总（本轮如实给出节点/引擎计数，不承诺未实现的估算数值）；
- **不假定任意两个工具都能连接**：端口类型 ``raster``/``vector``，
  生产者 output 声明 kind，消费者 input 声明期望 kind，错配在提交前拒绝；
- **跨进程对象节点默认物化文件**：上游产物以 committed 文件 URI 作为
  下游 DatasetRef 输入，**跨引擎（gdal→rasterio）同样经真实文件衔接**；
- **缓存引用输入指纹、完整参数、引擎与能力哈希及关键配置**：文件指纹
  分 fast（size+mtime_ns）与 strong（SHA-256）两级并记录复现等级；
  上游重跑导致其输出指纹变化时，下游缓存**自动失效**重新执行；
- **失败/恢复**：节点失败 → 其全部下游显式 pending（skipped，不冒充），
  独立分支按策略继续；已验证成功的物化成果在输入/参数/引擎 node_key 与
  输出指纹全部不变时，重跑直接以 ``cached`` 状态复用（恢复语义）。

限制
----
- 引擎执行必须由调用方注入（``execute: spec dict -> TaskStatus | TaskResult``，
  与批处理 P7-03 同约定；返回 TaskResult 时 error 详情会进入报告）；本模块
  不发进程，保证「工作流/批处理/菜单共用同一能力注册表 + 任务协议 +
  provider 适配器」（AGENTS）；
- 默认串行：同进程内管线优化属后续；
- 缓存 manifest 每轮运行结束整份覆写（崩溃后下一轮从最近的整份恢复，
  无增量写放大；增量落盘属后续）；
- 外部数据库/远程来源暂无本机稳定标识，缓存只覆盖本机文件。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

__all__ = [
    "WorkflowError",
    "WorkflowValidationError",
    "WorkflowReport",
    "load_workflow_document",
    "render_workflow_nodes",
    "run_workflow",
    "write_report",
]

#: provider → (runtime_id, expected_version) 缺省引擎约束；节点可显式覆盖。
_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "gdal": ("gdal-qgis", "3.13.3"),
    "rasterio": ("rasterio-venv", "1.5.2"),
}

#: 端口类型集合（跨进程对象节点物化为文件；其他引擎对象类型后续补充）。
_PORT_KINDS = {"raster", "vector"}

WorkflowRunner = Callable[[dict[str, Any]], Any]  # TaskStatus 或 TaskResult


class WorkflowError(ValueError):
    """工作流错误基类。"""


class WorkflowValidationError(WorkflowError):
    """提交前校验错误（环、端口错配、路径冲突、引用缺失、未登记 provider）。"""


class WorkflowReport:
    """工作流运行终态报告；每个节点保留诚实状态与缓存解释。"""

    def __init__(
        self,
        *,
        workflow_id: str,
        node_ids: list[str],
        continue_on_error: bool,
        engines: dict[str, int],
    ) -> None:
        self.workflow_id = workflow_id
        self.continue_on_error = bool(continue_on_error)
        self.engines = dict(engines)
        self.order: list[str] = list(node_ids)
        self.entries: dict[str, dict[str, Any]] = {
            nid: {
                "id": nid,
                "task_id": None,
                "status": "pending",
                "duration": None,
                "outputs": [],
                "error": None,
                "cache": {"hit": False, "reason": None},
            }
            for nid in node_ids
        }

    def mark(self, node_id: str, **fields: Any) -> None:
        self.entries[node_id].update(fields)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return counts

    def all_terminal(self) -> bool:
        return all(
            entry["status"] in ("succeeded", "failed", "cancelled", "cached", "pending")
            for entry in self.entries.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workflow_id": self.workflow_id,
            "continue_on_error": self.continue_on_error,
            "engines": self.engines,
            "summary": self.summary(),
            "order": self.order,
            "entries": [self.entries[nid] for nid in self.order],
            "all_terminal": self.all_terminal(),
        }


def load_workflow_document(path: Path | str) -> dict[str, Any]:
    """读工作流 JSON：顶层必须对象且 nodes 为非空列表；错则显式拒绝。"""
    src = Path(path)
    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"工作流文件不存在：{src}") from exc
    except JSONDecodeError as exc:
        raise WorkflowError(f"工作流文件不是合法 JSON：{src}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise WorkflowValidationError("工作流顶层必须是对象")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowValidationError("工作流缺少非空 nodes 列表")
    return raw


def _as_uri(p: str) -> str:
    return Path(p).resolve().as_uri()


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("file:"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        return Path(url2pathname(urlparse(uri).path))
    return Path(uri)


def _fingerprint_file(path: Path, *, strong: bool) -> dict[str, Any]:
    """文件指纹：fast = size + mtime_ns；strong 追加 sha256（记录复现等级）。

    大文件默认不全文件哈希（计划 §11）；strong 是显式选择的强校验选项。
    """
    if not path.is_file():
        raise WorkflowError(f"指纹目标不存在：{path}")
    st = path.stat()
    fp: dict[str, Any] = {"level": "strong" if strong else "fast"}
    if strong:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        fp["sha256"] = h.hexdigest()
        fp["size"] = st.st_size
    else:
        fp["size"] = st.st_size
        fp["mtime_ns"] = st.st_mtime_ns
    return fp


def _fingerprints_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    if a.get("level") == "strong" and b.get("level") == "strong":
        return all(a.get(k) == b.get(k) for k in ("sha256", "size"))
    return all(a.get(k) == b.get(k) for k in ("size", "mtime_ns"))


def _node_key(
    node: dict[str, Any],
    engine: EngineConstraint,
    input_refs: dict[str, dict[str, str]],
) -> str:
    """节点签名：引擎 + 能力 + 完整参数 + 输入引用（URI/kind）+ 输出键集合。"""
    payload = {
        "capability_id": node["capability_id"],
        "engine": engine.model_dump(mode="json"),
        "params": node["params"],
        "inputs": {
            port: {"uri": ref["uri"], "kind": ref["kind"]}
            for port, ref in sorted(input_refs.items())
        },
        "outputs": sorted(node["outputs"].keys()),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_node(raw: Any, index: int, seen_ids: set[str]) -> dict[str, Any]:
    """单个节点解析：id 唯一、provider 已登记、端口类型化。"""
    if not isinstance(raw, dict):
        raise WorkflowValidationError(f"nodes[{index}] 必须是对象")
    nid = raw.get("id")
    if not isinstance(nid, str) or not nid.strip():
        raise WorkflowValidationError(f"nodes[{index}] 缺少非空 id")
    nid = nid.strip()
    if nid in seen_ids:
        raise WorkflowValidationError(f"节点 id 重复：{nid}")
    capability_id = raw.get("capability_id")
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise WorkflowValidationError(f"节点 {nid} 缺少 capability_id")
    capability_id = capability_id.strip()

    provider = raw.get("engine_provider")
    provider = "gdal" if provider is None else provider
    if not isinstance(provider, str) or provider.strip() not in _PROVIDER_DEFAULTS:
        known = ", ".join(sorted(_PROVIDER_DEFAULTS))
        raise WorkflowValidationError(
            f"节点 {nid} 的 engine_provider={provider!r} 未登记（可选：{known}）"
        )
    provider = provider.strip()

    outputs_raw = raw.get("outputs")
    if not isinstance(outputs_raw, dict) or not outputs_raw:
        raise WorkflowValidationError(f"节点 {nid} 缺少 outputs（对象）")
    outputs: dict[str, dict[str, Any]] = {}
    for key, spec in outputs_raw.items():
        if isinstance(spec, str):
            spec = {"uri": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("uri"), str):
            raise WorkflowValidationError(f"节点 {nid} 输出 {key!r} 缺少 uri")
        kind = spec.get("kind", "raster")
        if kind not in _PORT_KINDS:
            raise WorkflowValidationError(
                f"节点 {nid} 输出 {key!r} 端口类型 {kind!r} 不在 {sorted(_PORT_KINDS)}"
            )
        outputs[str(key)] = {"uri": _as_uri(spec["uri"]), "kind": kind}

    inputs_raw = raw.get("inputs")
    if not isinstance(inputs_raw, dict):
        raise WorkflowValidationError(f"节点 {nid} 缺少 inputs（对象）")
    inputs: dict[str, dict[str, Any]] = {}
    for port, entry in inputs_raw.items():
        if isinstance(entry, str):
            entry = ["literal", entry]
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
            raise WorkflowValidationError(f"节点 {nid} 输入 {port!r} 格式错误")
        mode = entry[0]
        if mode not in ("literal", "node"):
            raise WorkflowValidationError(
                f"节点 {nid} 输入 {port!r} 只支持 literal/node，收到 {mode!r}"
            )
        if mode == "literal" and (len(entry) < 2 or not isinstance(entry[1], str)):
            raise WorkflowValidationError(f"节点 {nid} 输入 {port!r} literal 缺少路径")
        if mode == "node" and (
            len(entry) < 3 or not all(isinstance(x, str) for x in entry[1:3])
        ):
            raise WorkflowValidationError(
                f"节点 {nid} 输入 {port!r} node 需要 [node_id, output_key]"
            )
        declared_kind = entry[3] if len(entry) > 3 else "raster"
        if declared_kind not in _PORT_KINDS:
            raise WorkflowValidationError(
                f"节点 {nid} 输入 {port!r} 端口类型 {declared_kind!r} 不在 {sorted(_PORT_KINDS)}"
            )
        inputs[str(port)] = {
            "mode": mode,
            "node_id": entry[1] if mode == "node" else None,
            "output_key": entry[2] if mode == "node" else None,
            "path": entry[1] if mode == "literal" else None,
            "kind": declared_kind,
        }

    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise WorkflowValidationError(f"节点 {nid} params 必须是对象")

    return {
        "id": nid,
        "capability_id": capability_id,
        "engine_provider": provider,
        "engine_runtime": raw.get("engine_runtime"),
        "engine_version": raw.get("engine_version"),
        "task_prefix": str(raw.get("task_prefix") or "wf"),
        "driver": raw.get("driver"),
        "outputs": outputs,
        "inputs": inputs,
        "params": dict(params),
        "index": index,
    }


def render_workflow_nodes(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把文档解析并校验成拓扑序节点列表（校验失败显式拒绝）。

    提交前检查（计划 §11）：
    1. **环检测**：Kahn 拓扑；剩余入度 > 0 的节点即环成员，拒绝；
    2. **端口检查**：生产者声明端口类型 × 消费者期望类型，错配拒绝；
       同时拒绝「引用不存在的上游节点 / 输出键」；
    3. **路径冲突**：两个节点输出同一 URI，或输出 URI == 其他节点的字面
       输入 URI（写穿他人输入源）→ 拒绝；
    4. 资源汇总：按引擎计数的节点分布（不承诺未实现的估算数值）。
    """
    seen: set[str] = set()
    nodes_list: list[dict[str, Any]] = [
        parse_node(n, i, seen) for i, n in enumerate(doc["nodes"])
    ]
    by_id = {n["id"]: n for n in nodes_list}

    # --- 引用完整性 + 端口类型检查 ---
    edges: list[tuple[str, str]] = []
    for n in nodes_list:
        for port, entry in n["inputs"].items():
            if entry["mode"] == "literal":
                continue
            up_id, up_key = entry["node_id"], entry["output_key"]
            if up_id not in by_id:
                raise WorkflowValidationError(
                    f"节点 {n['id']} 的 {port} 引用不存在的上游节点 {up_id!r}"
                )
            up = by_id[up_id]
            if up_key not in up["outputs"]:
                raise WorkflowValidationError(
                    f"节点 {n['id']} 的 {port} 引用 {up_id!r} 不存在的输出 {up_key!r}"
                )
            if up["outputs"][up_key]["kind"] != entry["kind"]:
                raise WorkflowValidationError(
                    f"端口错配：{up_id}.{up_key} 声明 {up['outputs'][up_key]['kind']}，"
                    f"而 {n['id']}.{port} 期望 {entry['kind']}"
                )
            edges.append((up_id, n["id"]))

    # --- 环检测（Kahn） ---
    indegree = {n["id"]: 0 for n in nodes_list}
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes_list}
    for src_nid, dst_nid in edges:
        adj[src_nid].append(dst_nid)
        indegree[dst_nid] += 1
    queue = [nid for nid, deg in indegree.items() if deg == 0]
    order: list[str] = []
    while queue:
        cur = queue.pop()
        order.append(cur)
        for nxt in adj[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes_list):
        stuck = sorted(nid for nid, deg in indegree.items() if deg > 0)
        raise WorkflowValidationError(f"存在环（涉及节点：{stuck}）")

    # --- 路径冲突：输出 URI 重复，或输出 == 他节点的字面输入 URI ---
    out_by_uri: dict[str, str] = {}
    for n in nodes_list:
        for key, spec in n["outputs"].items():
            if spec["uri"] in out_by_uri:
                raise WorkflowValidationError(
                    f"产出冲突：{out_by_uri[spec['uri']]} 与 {n['id']}.{key} "
                    f"输出同一 URI（{spec['uri']}）"
                )
            out_by_uri[spec["uri"]] = f"{n['id']}.{key}"
    for n in nodes_list:
        for port, entry in n["inputs"].items():
            if entry["mode"] != "literal":
                continue
            lit_uri = _as_uri(entry["path"])
            if lit_uri in out_by_uri:
                raise WorkflowValidationError(
                    f"写穿冲突：{n['id']}.{port} 的字面输入 {lit_uri} 同时是 "
                    f"节点 {out_by_uri[lit_uri]} 的输出目标"
                )

    engines: dict[str, int] = {}
    for n in nodes_list:
        engines[n["engine_provider"]] = engines.get(n["engine_provider"], 0) + 1
    ordered = [by_id[nid] for nid in order]
    return ordered, engines


def write_report(path: Path | str, payload: dict[str, Any]) -> Path:
    """把 workflow report / cache manifest 写为 JSON（UTF-8 无 BOM）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return target


def run_workflow(
    doc: dict[str, Any],
    execute: WorkflowRunner,
    *,
    workflow_id: str | None = None,
    continue_on_error: bool = True,
    cache_dir: Path | str | None = None,
    strong_check: bool = False,
    report_path: Path | str | None = None,
) -> WorkflowReport:
    """按拓扑序执行工作流（串行），支持缓存命中与失败传播。

    Args:
        doc: 工作流文档（``load_workflow_document`` 返回值或直接 dict）。
        execute: ``spec dict -> TaskStatus | TaskResult`` 的真实执行器（与批处理
            同约定；返回 TaskResult 时，其 ``status``/``error`` 会被如实转写进
            报告）；spec 由本模块按每个节点的引擎约束构造（TaskSpec 通道）。
        workflow_id: 显式指定，否则 uuid；用于缓存 manifest 文件名。
        continue_on_error: True=失败节点的下游 pending、独立分支继续；
            False=首个失败后全部剩余节点显式 pending。
        cache_dir: 缓存 manifest 目录（None=无持久缓存，全部重跑）。
        strong_check: True=输出指纹用 SHA-256（强校验）；False=size+mtime。
        report_path: 提供则把 ``WorkflowReport.to_dict()`` 落盘。

    恢复语义：缓存 manifest 中 ``status=succeeded`` 的节点，若 node_key、
    各输入指纹、各输出指纹全部不变且文件仍在 → 本轮 ``cached``（不执行
    worker），其输出继续供下游引用；任一项变化 → 缓存失效重执行。
    """
    wf_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
    ordered, engines = render_workflow_nodes(doc)
    report = WorkflowReport(
        workflow_id=wf_id,
        node_ids=[n["id"] for n in ordered],
        continue_on_error=continue_on_error,
        engines=engines,
    )

    cache_file: Path | None = (
        Path(cache_dir) / "workflow-cache.json" if cache_dir is not None else None
    )
    prev_cache: dict[str, dict[str, Any]] = {}
    if cache_file is not None and cache_file.is_file():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and isinstance(loaded.get("nodes"), dict):
                prev_cache = loaded["nodes"]
        except (JSONDecodeError, OSError):
            prev_cache = {}

    deps: dict[str, list[str]] = {n["id"]: [] for n in ordered}
    for n in ordered:
        for entry in n["inputs"].values():
            if entry["mode"] == "node" and entry["node_id"] not in deps[n["id"]]:
                deps[n["id"]].append(entry["node_id"])

    current_output_fp: dict[str, dict[str, dict[str, Any]]] = {}
    current_output_uri: dict[str, dict[str, str]] = {}
    new_cache: dict[str, dict[str, Any]] = {}

    def _resolve_input_fp(node_id: str, port: str, entry: dict[str, Any]) -> dict[str, Any]:
        if entry["mode"] == "literal":
            return _fingerprint_file(Path(entry["path"]), strong=strong_check)
        up_fp = current_output_fp.get(entry["node_id"], {}).get(entry["output_key"])
        if up_fp is None:
            raise WorkflowError(
                f"节点 {node_id}.{port} 的上游 {entry['node_id']} 尚无可用输出指纹"
            )
        return up_fp

    for node in ordered:
        nid = node["id"]
        if not continue_on_error and any(
            e["status"] in ("failed", "pending") for e in report.entries.values()
        ):
            report.mark(nid, status="pending", error={"code": "skipped", "message": "前序节点失败"})
            continue
        failed_up = [
            d
            for d in deps[nid]
            if report.entries[d]["status"] in ("failed", "pending")
        ]
        if continue_on_error and failed_up:
            report.mark(
                nid,
                status="pending",
                error={"code": "skipped", "message": f"上游失败：{', '.join(failed_up)}"},
            )
            continue

        runtime_default, version_default = _PROVIDER_DEFAULTS[node["engine_provider"]]
        engine = EngineConstraint(
            provider_id=node["engine_provider"],
            runtime_id=node["engine_runtime"] or runtime_default,
            expected_version=node["engine_version"] or version_default,
        )

        input_refs: dict[str, dict[str, str]] = {}
        input_fps: dict[str, dict[str, Any]] = {}
        try:
            for port, entry in node["inputs"].items():
                uri = (
                    _as_uri(entry["path"])
                    if entry["mode"] == "literal"
                    else current_output_uri.get(entry["node_id"], {}).get(
                        entry["output_key"], ""
                    )
                )
                input_refs[port] = {"uri": uri, "kind": entry["kind"]}
                input_fps[port] = _resolve_input_fp(nid, port, entry)
        except WorkflowError as exc:
            report.mark(
                nid,
                status="failed",
                error={"code": "fingerprint", "message": str(exc)},
            )
            continue
        node_key = _node_key(node, engine, input_refs)

        # ---- 缓存命中判定（恢复）----
        prev = prev_cache.get(nid)
        cache_hit = False
        if isinstance(prev, dict) and prev.get("status") == "succeeded":
            key_ok = prev.get("node_key") == node_key
            prev_inputs = prev.get("inputs") or {}
            inputs_ok = (
                set(prev_inputs) == set(node["inputs"])
                and all(
                    _fingerprints_equal(input_fps[p], prev_inputs.get(p))
                    for p in node["inputs"]
                )
            )
            outputs_ok = True
            prev_outputs = prev.get("outputs") or {}
            for key, _spec in node["outputs"].items():
                prev_out = prev_outputs.get(key)
                if not (isinstance(prev_out, dict) and isinstance(prev_out.get("uri"), str)):
                    outputs_ok = False
                    break
                try:
                    current = _fingerprint_file(
                        _uri_to_path(prev_out["uri"]), strong=strong_check
                    )
                except WorkflowError:
                    outputs_ok = False
                    break
                if not _fingerprints_equal(current, prev_out.get("fp")):
                    outputs_ok = False
                    break
            cache_hit = key_ok and inputs_ok and outputs_ok
        if cache_hit:
            assert prev is not None
            report.mark(
                nid,
                task_id=prev.get("task_id"),
                status="cached",
                outputs=[
                    out.get("uri", "") for out in prev_outputs.values() if out.get("uri")
                ],
                cache={"hit": True, "reason": "node_key/输入指纹/输出指纹未变"},
            )
            current_output_fp[nid] = {
                key: prev_outputs.get(key, {}).get("fp", {}) for key in node["outputs"]
            }
            current_output_uri[nid] = {
                key: prev_outputs.get(key, {}).get("uri", "") for key in node["outputs"]
            }
            new_cache[nid] = prev
            continue

        # ---- 执行（TaskSpec 通道；跨引擎全部真实进程通信）----
        started = time.monotonic()
        spec_inputs: dict[str, Any] = {}
        for port, ref in input_refs.items():
            spec_inputs[port] = DatasetRef(
                dataset_id=f"{nid}-{port}",
                kind=DatasetKind.raster if ref["kind"] == "raster" else DatasetKind.vector,
                uri=ref["uri"],
            ).model_dump(mode="json")
        output_plans = [
            OutputPlan(
                key=key,
                kind=DatasetKind.raster if ospec["kind"] == "raster" else DatasetKind.vector,
                uri=ospec["uri"],
                driver=node.get("driver"),
                overwrite=True,
            ).model_dump(mode="json")
            for key, ospec in node["outputs"].items()
        ]
        workspace = (
            str(_uri_to_path(next(iter(node["outputs"].values()))["uri"]).parent)
            if node["outputs"]
            else str(Path.cwd())
        )
        spec = TaskSpec.model_validate(
            {
                "task_id": f"{node['task_prefix']}-{nid}-{uuid.uuid4().hex[:6]}",
                "attempt": 1,
                "capability_id": node["capability_id"],
                "descriptor_revision": "1.0",
                "engine": engine.model_dump(mode="json"),
                "project_root": str(doc.get("project_root") or Path.cwd()),
                "workspace": workspace,
                "inputs": spec_inputs,
                "outputs": output_plans,
                "parameters": node["params"],
            }
        )
        spec_dict = spec.model_dump(mode="json")

        try:
            result = execute(spec_dict)
        except Exception as exc:  # noqa: BLE001 — real runner boundary
            report.mark(
                nid,
                task_id=spec_dict["task_id"],
                status="failed",
                duration=round(time.monotonic() - started, 4),
                error={"code": "runner_error", "message": str(exc)},
            )
            continue

        # runner 可返回 TaskStatus（枚举）或 TaskResult（含 error 详情）——归一化
        engine_error: dict[str, Any] | None = None
        if isinstance(result, TaskResult):
            engine_status = result.status
            engine_error = result.error
        else:
            engine_status = result
        duration = round(time.monotonic() - started, 4)
        if engine_status is TaskStatus.succeeded:
            outputs_map: dict[str, dict[str, Any]] = {}
            for key, ospec in node["outputs"].items():
                fp = _fingerprint_file(_uri_to_path(ospec["uri"]), strong=strong_check)
                outputs_map[key] = {"uri": ospec["uri"], "fp": fp}
            report.mark(
                nid,
                task_id=spec_dict["task_id"],
                status="succeeded",
                duration=duration,
                outputs=[v["uri"] for v in outputs_map.values()],
                cache={"hit": False, "reason": "cache_miss"},
            )
            current_output_fp[nid] = {k: v["fp"] for k, v in outputs_map.items()}
            current_output_uri[nid] = {k: v["uri"] for k, v in outputs_map.items()}
            new_cache[nid] = {
                "task_id": spec_dict["task_id"],
                "status": "succeeded",
                "node_key": node_key,
                "inputs": input_fps,
                "outputs": outputs_map,
                "level": "strong" if strong_check else "fast",
            }
        elif engine_status is TaskStatus.cancelled:
            report.mark(
                nid,
                task_id=spec_dict["task_id"],
                status="cancelled",
                duration=duration,
            )
        else:
            report.mark(
                nid,
                task_id=spec_dict["task_id"],
                status="failed",
                duration=duration,
                error=engine_error
                or {
                    "code": "task_error",
                    "message": f"state={getattr(engine_status, 'value', engine_status)}",
                },
            )

    if cache_file is not None:
        write_report(
            cache_file,
            {
                "schema_version": "1.0",
                "workflow_id": wf_id,
                "level": "strong" if strong_check else "fast",
                "nodes": new_cache,
            },
        )
    if report_path is not None:
        write_report(report_path, report.to_dict())
    return report
