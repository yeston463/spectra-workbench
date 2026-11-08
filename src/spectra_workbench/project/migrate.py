"""P7-01 项目 schema 迁移：旧版本 → 当前版本，显式、可审计。

设计（与 `project/model.py` 一致的极高保守哲学）：
- **迁移器只做一次**且**不改动语义**：把旧 schema 的字段**翻译**到当前 1.0 字段；
- 迁移器只处理**文档**（dict）层，不涉及具体类，避免把旧项目错误解析为新类型；
- 未知旧版本一律显式拒绝，绝不静默宽容；同时**不动** path_anchor=None 的
  过时文档（该状态由 P3-01 的 legacy 确认流程接管）；
- 迁移前后必须可审计：``legacy_to_current`` 返回的 dict 在 ``notes`` 字段
  显式标注「[legacy 0.9 迁移]」前缀，供 sidecar/审计使用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spectra_workbench.project.model import PROJECT_SCHEMA_VERSION, ProjectError

LEGACY_SCHEMA_VERSION = "0.9"

__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "ensure_supported_document",
    "legacy_to_current",
    "load_json_document",
    "migrate_document",
]


def _is_absolute(stored: str) -> bool:
    return stored.startswith(("/", "\\")) or ":" in stored


def legacy_to_current(raw: dict) -> dict:
    """把 legacy 0.9 项目文档翻译成当前 1.0 语义。

    legacy 0.9 字段（真实旧格式示例）::

        {
          "schema_version": "0.9",
          "layers": [
            {"src": "res/raster.tif", "label": "背景", "visible": true},
            {"src": "res/second.dat", "label": "第二层", "visible": false,
             "metadata": {...}}
          ],
          "comment": "legacy note",
          "rois": [...]
        }

    转换规则：
    - ``layers[].src`` -> ``layers[].source``；``label`` -> ``name``；
      ``visible`` 保留；``metadata`` -> ``metadata_snapshot``；
    - ``comment`` -> ``notes``（前缀 ``[legacy 0.9 迁移]`` 显式可审计）；
    - 显式新增 ``schema_version`` 与 ``path_anchor=None``（走 P3-01 legacy 流程）；
    - 未知键显式拒绝。
    """
    if not isinstance(raw, dict):
        raise ProjectError("legacy 项目文件顶层必须是对象")
    if raw.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise ProjectError(
            f"legacy_to_current 仅支持 {LEGACY_SCHEMA_VERSION}，"
            f"收到 {raw.get('schema_version')!r}"
        )
    raw_layers = raw.get("layers")
    if not isinstance(raw_layers, list):
        raise ProjectError("legacy 0.9 项目缺少 layers 列表")
    keys_ok = {"src", "label", "visible", "provider_hint", "metadata"}
    layers_out: list[dict[str, Any]] = []
    looked_up = {}
    for legacy_order, entry in enumerate(raw_layers):
        looked_up[legacy_order] = entry
        if not isinstance(entry, dict):
            raise ProjectError(f"legacy layers[{legacy_order}] 必须是对象")
        unknown = set(entry) - keys_ok
        if unknown:
            raise ProjectError(
                f"legacy layers[{legacy_order}] 含未知字段 {sorted(unknown)}"
            )
        src = entry.get("src")
        label = entry.get("label")
        if not isinstance(src, str):
            raise ProjectError(
                f"legacy layers[{legacy_order}] 缺少非空 src 路径"
            )
        if not src.strip():
            raise ProjectError(
                f"legacy layers[{legacy_order}] 缺少非空 src 路径"
            )
        if not isinstance(label, str):
            raise ProjectError(
                f"legacy layers[{legacy_order}] 缺少非空 label"
            )
        if not label.strip():
            raise ProjectError(
                f"legacy layers[{legacy_order}] 缺少非空 label"
            )
        visible = bool(entry.get("visible", True))
        src_stripped = src.strip()
        metadata = entry.get("metadata")
        metadata_snapshot = dict(metadata) if isinstance(metadata, dict) else {}
        provider_hint = entry.get("provider_hint")
        layers_out.append(
            {
                "layer_id": f"legacy_{legacy_order}",
                "name": label.strip(),
                "source": Path(src_stripped).as_posix()
                if _is_absolute(src_stripped)
                else src_stripped,
                # legacy 0.9 无 path_anchor 标记：绝对/相对都显式标记，
                # 走 P3-01 legacy 确认流程（不自动猜测）。
                "source_is_relative": not _is_absolute(src_stripped),
                "visible": visible,
                "order": legacy_order,
                "provider_hint": provider_hint,
                "metadata_snapshot": metadata_snapshot,
            }
        )

    comment = raw.get("comment")
    comment_text = comment if isinstance(comment, str) else ""
    notes = f"[legacy {LEGACY_SCHEMA_VERSION} 迁移] {comment_text}".strip()
    out: dict[str, Any] = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "path_anchor": None,
        "layers": layers_out,
        "active_layer_id": (layers_out[0]["layer_id"] if layers_out else None),
        "active_view_id": None,
        "notes": notes,
    }
    if isinstance(raw.get("rois"), list):
        out["rois"] = raw["rois"]
    return out


def migrate_document(raw: dict) -> dict:
    """把文档迁移到当前版本（显式、无副作用）。

    当前版本原样返回；legacy 0.9 经 ``legacy_to_current``；其余版本显式拒绝。
    """
    if not isinstance(raw, dict):
        raise ProjectError("项目文件顶层必须是对象")
    version = raw.get("schema_version")
    if version is None:
        raise ProjectError("项目文件缺少 schema_version")
    if version == PROJECT_SCHEMA_VERSION:
        return raw
    if version == LEGACY_SCHEMA_VERSION:
        return legacy_to_current(raw)
    raise ProjectError(
        f"暂不支持从 {version!r} 迁移到 {PROJECT_SCHEMA_VERSION!r}；"
        f"可用迁移源：{LEGACY_SCHEMA_VERSION} → {PROJECT_SCHEMA_VERSION}"
    )


def load_json_document(path: Path | str) -> dict:
    """读项目 JSON 文档（仅 dict），不含 schema 校验；损坏必须明报。"""
    src = Path(path)
    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ProjectError(f"项目文件不存在：{src}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectError(f"项目文件不是合法 JSON：{src}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ProjectError("项目文件顶层必须是对象")
    return raw


def ensure_supported_document(raw: dict) -> dict:
    """把文档迁移/验证为当前版本；返回迁移后文档。"""
    return migrate_document(raw)
