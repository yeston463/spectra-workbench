"""能力基准模型、序列化、SHA-256 哈希校验与加载器（P2-01）。

契约保证：
1. 目标引擎版本固定（GDAL 3.13.3, Rasterio 1.5.2），有明确官方来源、文档 URL 与上游标签。
2. 基准条目具备确定性 JSON 规范化表达，计算精确 64 位十六进制 SHA-256 哈希。
3. 每个 CapabilityDescriptor.source.snapshot_sha256 与基准哈希严格一致，严禁伪造。
4. 提供从 contracts/baselines/ 或 resources/baselines/ 的静态加载与校验。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spectra_workbench.domain.models import CapabilityDescriptor


@dataclass(frozen=True)
class BaselineMetadata:
    """能力基准元数据，对应一份官方引擎能力的固定快照。"""

    baseline_id: str
    provider_id: str
    upstream_version: str
    target_version: str
    documentation_url: str
    repository_url: str
    upstream_tag: str
    release_date: str
    generated_at: str
    items_sha256: str
    capability_count: int
    completeness: str = "partial"
    generator_version: str = "1.0.0"
    raw_source_sha256: str | None = None
    runtime_inventory_sha256: str | None = None
    normalized_catalog_sha256: str | None = None


def compute_capabilities_sha256(descriptors: list[CapabilityDescriptor]) -> str:
    """计算能力清单的确定性 SHA-256 签名。

    排序规则：按 capability_id 字母升序排序后，输出紧凑 JSON（无多余空白，key 排序）。
    输出格式为标准 64 位小写十六进制字符串。
    """
    sorted_items = sorted(descriptors, key=lambda c: c.capability_id)
    canonical_list = []
    for c in sorted_items:
        dumped = c.model_dump(mode="json")
        # 排除 snapshot_sha256 自身，防止自引用哈希死锁
        if "source" in dumped and isinstance(dumped["source"], dict):
            src = dict(dumped["source"])
            src["snapshot_sha256"] = None
            dumped["source"] = src
        canonical_list.append(dumped)

    canonical_json = json.dumps(
        canonical_list,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def serialize_baseline(
    baseline_id: str,
    provider_id: str,
    upstream_version: str,
    target_version: str,
    documentation_url: str,
    repository_url: str,
    upstream_tag: str,
    release_date: str,
    descriptors: list[CapabilityDescriptor],
    generated_at: str = "2026-11-04T00:00:00Z",
    completeness: str = "partial",
    raw_source_sha256: str | None = None,
    runtime_inventory_sha256: str | None = None,
    generator_version: str = "1.0.0",
) -> dict[str, Any]:
    """将能力描述符列表组装为包含 SHA-256 快照签名的基准字典。"""
    # 1. 首先计算基础签名
    snapshot_hash = compute_capabilities_sha256(descriptors)

    # 2. 将 snapshot_sha256 回填到所有能力描述符的 source 字段
    updated_descriptors: list[dict[str, Any]] = []
    sorted_items = sorted(descriptors, key=lambda c: c.capability_id)
    for c in sorted_items:
        d = c.model_dump(mode="json")
        if "source" in d and isinstance(d["source"], dict):
            d["source"]["snapshot_sha256"] = snapshot_hash
            d["source"]["baseline_id"] = baseline_id
            d["source"]["upstream_version"] = upstream_version
        updated_descriptors.append(d)

    return {
        "baseline_id": baseline_id,
        "provider_id": provider_id,
        "upstream_version": upstream_version,
        "target_version": target_version,
        "completeness": completeness,
        "generator_version": generator_version,
        "source": {
            "documentation_url": documentation_url,
            "repository_url": repository_url,
            "upstream_tag": upstream_tag,
            "release_date": release_date,
            "raw_source_sha256": raw_source_sha256,
            "runtime_inventory_sha256": runtime_inventory_sha256,
        },
        "generated_at": generated_at,
        "items_sha256": snapshot_hash,
        "normalized_catalog_sha256": snapshot_hash,
        "capabilities": updated_descriptors,
    }


def load_baseline_from_dict(data: dict[str, Any]) -> tuple[BaselineMetadata, list[CapabilityDescriptor]]:
    """从字典反序列化基准，并严格校验 SHA-256 签名与模型契约。"""
    meta_dict = data.get("source", {})
    expected_hash = data.get("items_sha256", "")

    raw_caps = data.get("capabilities", [])
    descriptors = [CapabilityDescriptor.model_validate(c) for c in raw_caps]

    # 重新计算并核对哈希
    actual_hash = compute_capabilities_sha256(descriptors)
    if actual_hash != expected_hash:
        raise ValueError(
            f"基准 SHA-256 签名不匹配！期望: {expected_hash}, 实际重算: {actual_hash}。"
            f"基准可能被非法篡改或版本不一致。"
        )

    metadata = BaselineMetadata(
        baseline_id=str(data["baseline_id"]),
        provider_id=str(data["provider_id"]),
        upstream_version=str(data["upstream_version"]),
        target_version=str(data["target_version"]),
        documentation_url=str(meta_dict.get("documentation_url", "")),
        repository_url=str(meta_dict.get("repository_url", "")),
        upstream_tag=str(meta_dict.get("upstream_tag", "")),
        release_date=str(meta_dict.get("release_date", "")),
        generated_at=str(data.get("generated_at", "")),
        items_sha256=expected_hash,
        capability_count=len(descriptors),
        completeness=str(data.get("completeness", "partial")),
        generator_version=str(data.get("generator_version", "1.0.0")),
        raw_source_sha256=meta_dict.get("raw_source_sha256"),
        runtime_inventory_sha256=meta_dict.get("runtime_inventory_sha256"),
        normalized_catalog_sha256=str(data.get("normalized_catalog_sha256", expected_hash)),
    )
    return metadata, descriptors


def load_baseline_from_file(file_path: Path | str) -> tuple[BaselineMetadata, list[CapabilityDescriptor]]:
    """从本地 JSON 文件读取并校验能力基准。"""
    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"基准文件不存在: {p}")
    content = p.read_text(encoding="utf-8-sig")
    data = json.loads(content)
    return load_baseline_from_dict(data)
