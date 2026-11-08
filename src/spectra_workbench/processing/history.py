"""P7-05 历史与可复现导出：SQLite 运行历史 + 参数重开 + 可执行配方。

语义（对齐计划 §11/§12 与验收「从历史重开参数；记录输入/版本/关键配置；
导出的执行入口跑出同类成果」）：
- **统一任务通道**：历史只记录既有 TaskSpec 通道的真实执行（``record``
  记单任务/批处理条目/工作流节点同一结构），不另起计算实现；
- **记录**：输入（端口 → URI/kind + 快速指纹 size+mtime，可选 SHA-256 强
  校验）、版本（spec 引擎约束 + **worker 上报的真实引擎版本**，计划 §12
  ``engine_runtime`` 列）、关键配置（descriptor_revision、完整参数 JSON、
  状态/时长/错误）；
- **从历史重开参数**：``reopen_params`` 还原可再次提交的请求（能力/引擎/
  参数/输入 URI/kind），用于重提任务；
- **导出执行入口**：``export_workflow_recipe`` 把若干历史记录导出为标准
  **workflow 文档**（P7-04 ``run_workflow`` 为唯一执行入口），直接回放跑
  出同类成果（数值等价验收由调用方/测试完成）。

SQLite 约束（计划 §12）：单一应用服务写入（模块自持连接，由调用方保证
串行），不做多 worker 共同写日志；``schema_version`` 显式迁移，旧版本可
升级到当前；遇到**未知更高版本**拒绝打开（绝不静默降级读写）。

限制
----
- 指纹默认快速（size+mtime_ns，与 P7-04 一致），强校验按需显式；
- 指纹只覆盖本机可落文件；远程/外库来源按 URI 原样记录、不入强校验。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

__all__ = ["HistoryError", "HistoryStore"]

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    group_id TEXT,
    capability_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    duration_seconds REAL,
    project_root TEXT,
    descriptor_revision TEXT,
    engine_provider TEXT,
    engine_runtime_id TEXT,
    engine_version_expected TEXT,
    engine_version_actual TEXT,
    params_json TEXT NOT NULL,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    port TEXT NOT NULL,
    uri TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    PRIMARY KEY (run_id, port)
);
CREATE TABLE IF NOT EXISTS run_outputs (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    out_key TEXT NOT NULL,
    uri TEXT NOT NULL,
    kind TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    PRIMARY KEY (run_id, out_key)
);
"""


class HistoryError(ValueError):
    """历史存储错误（找不到/结构错/版本无法迁移）。"""


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("file:"):
        return Path(url2pathname(urlparse(uri).path))
    return Path(unquote(uri))


def _file_fingerprint(path: Path, *, strong: bool) -> dict[str, Any]:
    """快速指纹 size+mtime_ns；strong 追加 sha256（复现等级显式记录）。"""
    st = path.stat()
    fp: dict[str, Any] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if strong:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        fp["sha256"] = h.hexdigest()
    return fp


class HistoryStore:
    """SQLite 运行历史：单一写入方，必要时执行 schema 迁移。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        version = self.schema_version()
        if version > SCHEMA_VERSION:
            raise HistoryError(
                f"历史库 schema_version={version} 高于当前支持 {SCHEMA_VERSION}，"
                f"拒绝打开：{self.path}"
            )
        if version < SCHEMA_VERSION:
            self._migrate(version)
        assert self.schema_version() == SCHEMA_VERSION

    # ---------------------------------------------------------------- 打开/迁移
    def schema_version(self) -> int:
        """读 meta 中的 schema_version；空库返回 0，坏值显式拒绝。"""
        try:
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
        except sqlite3.OperationalError:
            return 0
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise HistoryError(f"schema_version 非整数：{row!r}") from exc

    def _migrate(self, from_version: int) -> None:
        """升级到当前 schema：v0 空库建 v1；后续版本出现时在此追加步骤。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ record
    def record(
        self,
        spec_dict: dict[str, Any],
        result: Any,
        *,
        kind: str = "task",
        group_id: str | None = None,
        strong: bool = False,
    ) -> str:
        """记录一次真实执行（TaskSpec 通道）。

        Args:
            spec_dict: ``TaskSpec.model_dump(mode="json")`` 结构。
            result: TaskResult（真实 worker 终态与 engine_runtime 真实版本）。
            kind: task / batch_item / workflow_node 等显示分类。
            group_id: 批处理或工作流运行 id（用于按组重开）。
            strong: 输入用 SHA-256 强指纹（默认快速指纹）。

        Returns:
            run_id（uuid 形如 ``run-<hex16>``）。
        """
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        status = getattr(result.status, "value", str(result.status))
        engine = spec_dict["engine"]

        input_rows: list[tuple[Any, ...]] = []
        for port, ref in (spec_dict.get("inputs") or {}).items():
            refs = ref if isinstance(ref, list) else [ref]
            for i, r in enumerate(refs):
                port_name = port if len(refs) == 1 else f"{port}#{i}"
                p = _uri_to_path(r["uri"])
                fp = _file_fingerprint(p, strong=strong) if p.is_file() else {}
                input_rows.append(
                    (
                        run_id,
                        port_name,
                        r["uri"],
                        r.get("kind", "raster"),
                        fp.get("size"),
                        fp.get("mtime_ns"),
                        fp.get("sha256"),
                    )
                )

        output_rows: list[tuple[Any, ...]] = []
        for out in result.outputs or []:
            seen: set[str] = set()
            uris = [out.get("uri", "")] + list(out.get("committed_files") or [])
            for uri in uris:
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                p = _uri_to_path(uri)
                fp = _file_fingerprint(p, strong=strong) if p.is_file() else {}
                output_rows.append(
                    (
                        run_id,
                        str(out.get("key", "output")),
                        uri,
                        out.get("data_type"),
                        fp.get("size"),
                        fp.get("mtime_ns"),
                        fp.get("sha256"),
                    )
                )

        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        engine_actual = (result.engine_runtime or {}).get("version")
        self._conn.execute(
            "INSERT INTO runs (run_id, kind, group_id, capability_id, task_id, attempt,"
            " status, created_at, duration_seconds, project_root, descriptor_revision,"
            " engine_provider, engine_runtime_id, engine_version_expected,"
            " engine_version_actual, params_json, error_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                group_id,
                spec_dict["capability_id"],
                spec_dict["task_id"],
                spec_dict.get("attempt", 1),
                status,
                created,
                result.duration_seconds,
                spec_dict.get("project_root", ""),
                spec_dict.get("descriptor_revision", ""),
                engine.get("provider_id"),
                engine.get("runtime_id"),
                engine.get("expected_version"),
                engine_actual,
                json.dumps(spec_dict.get("parameters") or {}, ensure_ascii=False),
                json.dumps(result.error, ensure_ascii=False) if result.error else None,
            ),
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO run_inputs"
            " (run_id, port, uri, kind, size, mtime_ns, sha256) VALUES (?,?,?,?,?,?,?)",
            input_rows,
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO run_outputs"
            " (run_id, out_key, uri, kind, size, mtime_ns, sha256) VALUES (?,?,?,?,?,?,?)",
            output_rows,
        )
        self._conn.commit()
        return run_id

    # -------------------------------------------------------------------- 查询
    def get(self, run_id: str) -> dict[str, Any]:
        cur = self._conn.execute(
            "SELECT run_id, kind, group_id, capability_id, task_id, attempt, status,"
            " created_at, duration_seconds, project_root, descriptor_revision,"
            " engine_provider, engine_runtime_id, engine_version_expected,"
            " engine_version_actual, params_json, error_json FROM runs"
            " WHERE run_id = ?",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HistoryError(f"历史记录不存在：{run_id}")
        (
            rid, kind, group_id, capability_id, task_id, attempt, status, created,
            duration, project_root, descriptor_revision, provider, runtime_id,
            expected, actual, params_json, error_json,
        ) = row
        inputs = self._conn.execute(
            "SELECT port, uri, kind, size, mtime_ns, sha256 FROM run_inputs"
            " WHERE run_id = ? ORDER BY port",
            (run_id,),
        ).fetchall()
        outputs = self._conn.execute(
            "SELECT out_key, uri, kind, size, mtime_ns, sha256 FROM run_outputs"
            " WHERE run_id = ? ORDER BY out_key",
            (run_id,),
        ).fetchall()
        return {
            "run_id": rid,
            "kind": kind,
            "group_id": group_id,
            "capability_id": capability_id,
            "task_id": task_id,
            "attempt": attempt,
            "status": status,
            "created_at": created,
            "duration_seconds": duration,
            "project_root": project_root,
            "descriptor_revision": descriptor_revision,
            "engine": {
                "provider_id": provider,
                "runtime_id": runtime_id,
                "expected_version": expected,
                "actual_version": actual,
            },
            "params": json.loads(params_json),
            "error": json.loads(error_json) if error_json else None,
            "inputs": [
                {"port": p, "uri": u, "kind": k, "size": s, "mtime_ns": m, "sha256": sh}
                for (p, u, k, s, m, sh) in inputs
            ],
            "outputs": [
                {"key": k, "uri": u, "kind": kk, "size": s, "mtime_ns": m, "sha256": sh}
                for (k, u, kk, s, m, sh) in outputs
            ],
        }

    def runs_by_group(self, group_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT run_id FROM runs WHERE group_id = ? ORDER BY run_id", (group_id,)
        )
        return [self.get(r[0]) for r in cur.fetchall()]

    def reopen_params(self, run_id: str) -> dict[str, Any]:
        """从历史重开参数：还原可重新提交的请求（不含输出路径与输出指纹）。"""
        rec = self.get(run_id)
        inputs: dict[str, Any] = {}
        for row in rec["inputs"]:
            port = row["port"]
            entry = {"uri": row["uri"], "kind": row["kind"]}
            if port in inputs:
                prev = inputs[port]
                inputs[port] = (
                    [prev, entry] if not isinstance(prev, list) else [*prev, entry]
                )
            else:
                inputs[port] = entry
        return {
            "run_id": rec["run_id"],
            "kind": rec["kind"],
            "capability_id": rec["capability_id"],
            "engine": rec["engine"],
            "project_root": rec["project_root"],
            "descriptor_revision": rec["descriptor_revision"],
            "params": rec["params"],
            "status": rec["status"],
            "inputs": inputs,
        }

    # ------------------------------------------------------------- 配方导出
    def export_workflow_recipe(
        self,
        run_ids: list[str],
        path: Path | str,
        *,
        strong: bool = False,
    ) -> dict[str, Any]:
        """把历史记录导出为**标准 workflow 文档**（P7-04 ``run_workflow`` 执行入口）。

        每条记录一个节点：capability/engine/params 取自历史；输入为字面 URI
        （kind 原样）；输出沿用记录 URI（run_workflow 语义 overwrite=True）。
        strong=True 时在节点 params 之外无影响——导出仅是文档；strong 校验
        属 run_workflow 的指纹选项。返回落盘的文档 dict 供断言/直接回放。
        """
        nodes: list[dict[str, Any]] = []
        out_owner: dict[str, tuple[str, str, str]] = {}  # 本地路径 -> (node_id, key, kind)
        for i, run_id in enumerate(run_ids):
            rec = self.get(run_id)
            engine = rec["engine"]
            node_id = f"h{i:02d}_{rec['capability_id'].replace(':', '_')}"
            inputs: dict[str, list[str]] = {}
            for row in rec["inputs"]:
                if row["kind"] not in ("raster", "vector") or not row["uri"]:
                    continue
                # 工作流文档的 uri 字段是**本地路径**（parse_node 会再 as_uri）：
                # 历史 row 是 file:// URI（含百分号编码），必须先解码回本地路径。
                inputs[row["port"]] = [
                    "literal",
                    str(_uri_to_path(row["uri"])),
                    row["kind"],
                ]
            outputs: dict[str, dict[str, str]] = {}
            for row in rec["outputs"]:
                if (row["kind"] or "raster") not in ("raster", "vector") or not row["uri"]:
                    continue
                local_path = str(_uri_to_path(row["uri"]))
                outputs[row["key"]] = {"uri": local_path, "kind": row["kind"] or "raster"}
            nodes.append(
                {
                    "id": node_id,
                    "capability_id": rec["capability_id"],
                    "engine_provider": engine["provider_id"],
                    "engine_runtime": engine["runtime_id"],
                    "engine_version": engine["expected_version"],
                    "inputs": inputs,
                    "outputs": outputs,
                    "params": rec["params"],
                    "task_prefix": "hist",
                }
            )
            for key, spec in outputs.items():
                out_owner.setdefault(spec["uri"], (node_id, key, spec["kind"]))
        # 边合成：当某节点的字面输入 URI 恰是另一节点的输出 URI 时，改写为
        # 节点引用，保住 DAG 执行序（否则导出的图将退化为独立字面输入）。
        for node in nodes:
            for port, entry in node["inputs"].items():
                if not (isinstance(entry, list) and entry and entry[0] == "literal"):
                    continue
                owner = out_owner.get(entry[1])
                if owner is not None and owner[0] != node["id"]:
                    node["inputs"][port] = ["node", owner[0], owner[1], owner[2]]
        project_root = self.get(run_ids[0])["project_root"] if run_ids else ""
        doc = {
            "schema_version": "1.0",
            "project_root": project_root,
            "nodes": nodes,
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return doc
