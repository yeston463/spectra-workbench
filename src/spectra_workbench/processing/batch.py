"""P7-03 批处理：模板展开成多个 TaskSpec 并按序执行。

核心语义
--------
- **模板**：一段 JSON 描述 N 个任务（共享能力 ID、engine、公共参数与路径模板）；
- **多输入/多输出**：``inputs``/``outputs`` 列表每项为 ``{item[key]}`` 字符串，展开成
  逐项结果；单个输出 key 固定 ``output``，多个输出依次 ``output_0/output_1``；
- **参数变化**：``params`` 内字符串用 ``{item[key]}`` 展开；
- **同名冲突**：``overwrite`` 由调用方决定（false: 已存在目标 → 失败；
  true: 允许覆盖），由 P1 既有 commit_files 语义执行；
- **单项失败继续策略**：``continue_on_error=True`` 收集所有条目终态；
  ``False`` 遇到第 1 个失败立即停止，未处置项显式 ``pending``（不冒充已成功）。
- **manifest** 落盘由调用方负责（本模块仅返回 dict）。

限制
----
- 不并行：任务按序执行以避免输出侧写冲突；
- 不会改写用户提供的批处理 manifest。
"""

from __future__ import annotations

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
    TaskSpec,
    TaskStatus,
)

__all__ = [
    "BatchError",
    "BatchReport",
    "render_batch_specs",
    "run_batch",
    "write_manifest",
]

#: 批处理 runner 接口：给 (spec dict) → 终态 TaskStatus
BatchRunner = Callable[[dict[str, Any]], TaskStatus]


class BatchError(ValueError):
    """批处理解析/运行错误。"""


def load_batch_document(path: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读批处理清单 JSON：返回 (template, items)。文件损坏/结构错必须明报。"""
    src = Path(path)
    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise BatchError(f"批处理清单不存在：{src}") from exc
    except JSONDecodeError as exc:
        raise BatchError(f"批处理清单不是合法 JSON：{src}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise BatchError("批处理清单顶层必须是对象")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) == 0:
        raise BatchError("批处理清单缺少非空 items 列表")
    return raw, items


def _safe_format(template: Any, scope: dict[str, Any], *, tag: str = "value") -> str:
    """按 ``{item[key]}`` 展开字符串；展开失败必须显式报错。"""
    if not isinstance(template, str):
        return template
    try:
        return template.format(**scope)
    except (KeyError, IndexError, AttributeError) as exc:
        raise BatchError(f"{tag} 模板 {template!r} 展开失败：{exc}") from exc


def render_batch_specs(
    template: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    """把模板 + N 个 item 渲染成 N 份 TaskSpec (spec, item, index)。

    模板格式::

        {
          "capability_id": "gdal:translate",   # 必填
          "engine_provider": "gdal",           # 缺省 gdal
          "inputs": ["{item.src}"],            # 可选；空=无输入端口
          "outputs": ["{item.dst}"],           # 可选；空=无输出
          "params": {"dstNodata": -9999},      # 可选
          "overwrite": False,                  # 对应 OutputPlan.overwrite
          "task_prefix": "batch"               # task_id 前缀
        }
    """
    capability_id = template.get("capability_id")
    if not isinstance(capability_id, str) or not capability_id:
        raise BatchError("批处理模板缺少 capability_id")
    provider = template.get("engine_provider") or "gdal"
    if not isinstance(provider, str) or not provider:
        raise BatchError("批处理模板 engine_provider 必须为非空字符串")
    inputs_tpl = template.get("inputs") or []
    outputs_tpl = template.get("outputs") or []
    params_tpl = template.get("params") or {}
    task_prefix = str(template.get("task_prefix") or "batch")
    overwrite = bool(template.get("overwrite") or False)
    if not isinstance(inputs_tpl, (list, tuple)) or not all(
        isinstance(p, str) for p in inputs_tpl
    ):
        raise BatchError("inputs 必须是字符串列表")
    if not isinstance(outputs_tpl, (list, tuple)) or not all(
        isinstance(p, str) for p in outputs_tpl
    ):
        raise BatchError("outputs 必须是字符串列表")
    if not isinstance(params_tpl, dict):
        raise BatchError("params 必须是字典")

    scope_map: dict[str, Any] = {"item": {}}
    specs: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    project_root = str(template.get("project_root") or "")
    if not project_root:
        raise BatchError("批处理模板 project_root 必须为非空字符串")

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError(f"items[{index}] 必须是对象")
        scope_map = {"item": item}
        in_paths = [_safe_format(p, scope_map, tag="inputs") for p in inputs_tpl]
        out_paths = [_safe_format(p, scope_map, tag="outputs") for p in outputs_tpl]
        params = {
            key: _safe_format(value, scope_map, tag="params")
            if isinstance(value, str)
            else value
            for key, value in params_tpl.items()
        }

        inputs: dict[str, list[dict[str, Any]] | dict[str, Any]] = {}
        if in_paths:
            refs = [
                DatasetRef(
                    dataset_id=f"in-{i}",
                    kind=DatasetKind.raster,
                    uri=Path(p).resolve().as_uri(),
                ).model_dump(mode="json")
                for i, p in enumerate(in_paths)
            ]
            if len(refs) == 1:
                inputs["input"] = refs[0]
            else:
                inputs["input"] = refs
        outputs = [
            OutputPlan(
                key=(f"output_{i}" if len(out_paths) > 1 else "output"),
                kind=DatasetKind.raster,
                uri=Path(p).resolve().as_uri(),
                overwrite=overwrite,
            ).model_dump(mode="json")
            for i, p in enumerate(out_paths)
        ]
        workspace = str(
            Path(out_paths[0]).parent
            if out_paths
            else Path(in_paths[0]).parent
            if in_paths
            else Path.cwd()
        )

        spec = TaskSpec.model_validate(
            {
                "task_id": f"{task_prefix}-{index:03d}-{uuid.uuid4().hex[:8]}",
                "attempt": 1,
                "capability_id": capability_id,
                "descriptor_revision": str(template.get("descriptor_revision") or "1.0"),
                "engine": EngineConstraint(
                    provider_id=provider,
                    runtime_id=str(template.get("engine_runtime") or "gdal-qgis"),
                    expected_version=str(template.get("engine_version") or "3.13.3"),
                ).model_dump(mode="json"),
                "project_root": project_root,
                "workspace": workspace,
                "inputs": inputs,
                "outputs": outputs,
                "parameters": params,
            }
        )
        specs.append((spec.model_dump(mode="json"), item, index))
    return specs


def write_manifest(path: Path | str, payload: dict[str, Any]) -> Path:
    """把 batch manifest 写为 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return target


class BatchReport:
    """批处理终态；entries 保留成功/失败/pending 的诚实终态与 duration。"""

    def __init__(self, *, batch_id: str, items: list[dict[str, Any]], continue_on_error: bool) -> None:
        self.batch_id = batch_id
        self.entries: list[dict[str, Any]] = [
            {
                "index": i,
                "task_id": None,
                "status": "pending",
                "duration": None,
                "outputs": [],
                "error": None,
            }
            for i in range(len(items))
        ]
        self.continue_on_error = bool(continue_on_error)

    def mark(self, index: int, **fields: Any) -> None:
        self.entries[index].update(fields)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return counts

    def all_terminal(self) -> bool:
        return all(
            entry["status"] in ("succeeded", "failed", "cancelled")
            for entry in self.entries
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "continue_on_error": self.continue_on_error,
            "summary": self.summary(),
            "entries": self.entries,
            "all_terminal": self.all_terminal(),
        }


def run_batch(
    template: dict[str, Any],
    items: list[dict[str, Any]],
    execute: BatchRunner,
    *,
    continue_on_error: bool = True,
    batch_id: str | None = None,
) -> BatchReport:
    """按批处理清单顺序执行（严格串行）。

    Args:
        template: 模板（见 render_batch_specs）。
        items: 每项 dict，可含 N 键值供 format 展开到 inputs/outputs/params。
        execute: **返回 TaskStatus** 的调用器（如真实 worker/scheduler）。
        continue_on_error: true=失败继续下一条；false=遇失败立即停止。
        batch_id: 可显式指定，否则 uuid 生成。

    返回 BatchReport（含每个条目的 task_id/inputs/outputs/终态/duration/error）。
    """
    batch = batch_id or uuid.uuid4().hex[:12]
    rendered = render_batch_specs(template, items)
    report = BatchReport(
        batch_id=batch, items=items, continue_on_error=continue_on_error
    )

    for entry_spec, _item, index in rendered:
        if not report.continue_on_error:
            previous = report.entries[:index]
            if any(e["status"] in ("failed", "cancelled") for e in previous):
                # 失败即停止；未处置项显式 pending（不冒充成功）
                report.mark(
                    index,
                    task_id=entry_spec["task_id"],
                    status="pending",
                    error={"code": "skipped", "message": "previous item failed"},
                )
                continue

        if entry_spec is None:
            raise BatchError("rendered spec is None")  # mypy narrowing

        started = time.monotonic()
        try:
            result = execute(entry_spec)
        except Exception as exc:  # noqa: BLE001 — real runner boundary
            report.mark(
                index,
                task_id=entry_spec["task_id"],
                status="failed",
                duration=round(time.monotonic() - started, 4),
                error={"code": "runner_error", "message": str(exc)},
            )
            continue

        duration = round(time.monotonic() - started, 4)
        if result is TaskStatus.succeeded:
            report.mark(index, task_id=entry_spec["task_id"], status="succeeded", duration=duration)
        elif result is TaskStatus.cancelled:
            report.mark(index, task_id=entry_spec["task_id"], status="cancelled", duration=duration)
        else:
            report.mark(
                index,
                task_id=entry_spec["task_id"],
                status="failed",
                duration=duration,
                error={"code": "task_error", "message": f"state={result.value}"},
            )
    return report
