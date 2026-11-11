"""能力注册表（CapabilityRegistry）与能力审计服务（P2-01 ~ P2-03）。

契约保证：
1. 统一管理 GDAL、Rasterio 及扩展引擎的所有公开能力基准与运行时发现能力。
2. 维护新旧命令双向别名索引（如 gdal_translate <-> gdal:translate, rio_convert <-> rio:cli:convert）。
3. 严格遵循四维状态独立判定：
   - 已发现 (discovered)：presence == present
   - 可调用 (callable)：presence == present 且 availability == ready 且 binding 具备执行入口（cli/form/script/specialized）
   - 已验证 (verified)：validation 为 smoke_passed 或 numerical_passed
   - 缺失能力 (missing)：presence == missing 或 availability 为 missing_dependency / unsupported / blocked
4. 审计报告基准分母非零，明确记录每一项缺失能力及其具体原因，严禁静默过滤。
5. 进程隔离：纯静态加载与查询，UI 进程中安全导入。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityKind,
    Presence,
    Validation,
)
from spectra_workbench.plugins.extensions import (
    build_optional_extension_descriptors,
)
from spectra_workbench.registry.baseline import (
    load_baseline_from_file,
    serialize_baseline,
)
from spectra_workbench.registry.gdal_index import (
    BASELINE_ID as GDAL_BASELINE_ID,
)
from spectra_workbench.registry.gdal_index import (
    GDAL_ALIASES,
    GDAL_DOC_BASE,
    GDAL_VERSION,
    build_gdal_baseline,
)
from spectra_workbench.registry.rasterio_index import (
    BASELINE_ID as RASTERIO_BASELINE_ID,
)
from spectra_workbench.registry.rasterio_index import (
    RASTERIO_ALIASES,
    RASTERIO_DOC_BASE,
    RASTERIO_VERSION,
    build_rasterio_baseline,
)


class CapabilityRegistry:
    """系统全局统一能力注册表。"""

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        # 别名映射：alias -> capability_id
        self._alias_to_id: dict[str, str] = {}
        # 反向别名映射：capability_id -> set of aliases
        self._id_to_aliases: dict[str, set[str]] = {}

    def register(
        self,
        descriptor: CapabilityDescriptor,
        aliases: Sequence[str] | None = None,
    ) -> None:
        """登记一个能力描述符，并可选登记别名。"""
        cid = descriptor.capability_id
        self._capabilities[cid] = descriptor
        if cid not in self._id_to_aliases:
            self._id_to_aliases[cid] = set()

        if aliases:
            for alias in aliases:
                self._alias_to_id[alias] = cid
                self._id_to_aliases[cid].add(alias)

    def register_batch(
        self,
        descriptors: Sequence[CapabilityDescriptor],
        alias_map: dict[str, str] | None = None,
    ) -> None:
        """批量登记能力描述符与别名字典。"""
        for desc in descriptors:
            self.register(desc)

        if alias_map:
            for alias, target_id in alias_map.items():
                if target_id in self._capabilities:
                    self._alias_to_id[alias] = target_id
                    self._id_to_aliases[target_id].add(alias)

    def unregister(self, capability_id: str) -> bool:
        """注销一个能力描述符及其关联别名。成功返回 True，不存在返回 False。"""
        if capability_id not in self._capabilities:
            return False
        del self._capabilities[capability_id]
        aliases = self._id_to_aliases.pop(capability_id, set())
        for a in aliases:
            self._alias_to_id.pop(a, None)
        return True

    def get(self, capability_id_or_alias: str) -> CapabilityDescriptor | None:
        """根据 ID 或别名获取能力描述符。"""
        if capability_id_or_alias in self._capabilities:
            return self._capabilities[capability_id_or_alias]
        real_id = self._alias_to_id.get(capability_id_or_alias)
        if real_id:
            return self._capabilities.get(real_id)
        return None

    def get_by_alias(self, alias: str) -> CapabilityDescriptor | None:
        """仅通过别名查找能力描述符。"""
        real_id = self._alias_to_id.get(alias)
        return self._capabilities.get(real_id) if real_id else None

    def get_aliases(self, capability_id: str) -> list[str]:
        """获取指定能力的所有关联别名列表。"""
        return sorted(list(self._id_to_aliases.get(capability_id, set())))

    def has(self, capability_id_or_alias: str) -> bool:
        """检查能力或别名是否存在于注册表中。"""
        return (
            capability_id_or_alias in self._capabilities
            or capability_id_or_alias in self._alias_to_id
        )

    def list_all(self) -> list[CapabilityDescriptor]:
        """按 capability_id 排序输出全部能力描述符。"""
        return [self._capabilities[k] for k in sorted(self._capabilities.keys())]

    def filter(
        self,
        provider: str | None = None,
        kind: CapabilityKind | None = None,
        category: str | None = None,
        presence: Presence | None = None,
        binding: Binding | None = None,
        availability: Availability | None = None,
        validation: Validation | None = None,
    ) -> list[CapabilityDescriptor]:
        """按引擎、类型、分类与四维状态灵活过滤能力清单。"""
        result = []
        for c in self.list_all():
            if provider and c.engine.provider_id != provider:
                continue
            if kind and c.kind != kind:
                continue
            if category and category not in c.category:
                continue
            if presence and c.state.presence != presence:
                continue
            if binding and c.state.binding != binding:
                continue
            if availability and c.state.availability != availability:
                continue
            if validation and c.state.validation != validation:
                continue
            result.append(c)
        return result

    # --- 四维状态独立查询 ---

    def discovered(self, provider: str | None = None) -> list[CapabilityDescriptor]:
        """查询已发现能力（presence == present）。"""
        return [c for c in self.filter(provider=provider) if c.state.presence == Presence.present]

    def callable(self, provider: str | None = None) -> list[CapabilityDescriptor]:
        """查询可调用能力（presence == present 且 availability == ready 且已具备可用绑定）。"""
        non_callable_bindings = {Binding.none, Binding.indexed}
        return [
            c
            for c in self.filter(provider=provider)
            if c.state.presence == Presence.present
            and c.state.availability == Availability.ready
            and c.state.binding not in non_callable_bindings
        ]

    def verified(self, provider: str | None = None) -> list[CapabilityDescriptor]:
        """查询已完成验证的能力（validation in (smoke_passed, numerical_passed)）。"""
        verified_vals = {Validation.smoke_passed, Validation.numerical_passed}
        return [c for c in self.filter(provider=provider) if c.state.validation in verified_vals]

    def platform_executable(self, provider: str | None = None) -> list[CapabilityDescriptor]:
        """查询平台已接线并具备 Adapter 执行器的能力（binding == Binding.form 且 ready）。"""
        return [c for c in self.callable(provider=provider) if c.state.binding == Binding.form]

    def missing(self, provider: str | None = None) -> list[CapabilityDescriptor]:
        """查询缺失或受阻能力（presence == missing 或 availability != ready）。保留缺失清单，严禁漏项。"""
        return [
            c
            for c in self.filter(provider=provider)
            if c.state.presence == Presence.missing
            or c.state.availability
            in (Availability.missing_dependency, Availability.unsupported, Availability.blocked)
        ]

    # --- 覆盖审计报告 ---

    def audit_report(self, provider: str | None = None) -> dict[str, Any]:
        """生成能力覆盖审计报告，输出基准分母、各维度数量、比例及缺失清单。"""
        all_caps = self.filter(provider=provider)
        total_count = len(all_caps)

        discovered_caps = self.discovered(provider=provider)
        callable_caps = self.callable(provider=provider)
        platform_exec_caps = self.platform_executable(provider=provider)
        verified_caps = self.verified(provider=provider)
        missing_caps = self.missing(provider=provider)

        missing_details = [
            {
                "capability_id": c.capability_id,
                "name": c.name,
                "provider": c.engine.provider_id,
                "kind": c.kind.value,
                "presence": c.state.presence.value,
                "availability": c.state.availability.value,
                "binding": c.state.binding.value,
                "limitations": c.limitations,
            }
            for c in missing_caps
        ]

        def _safe_ratio(count: int, total: int) -> float:
            return round(count / total, 4) if total > 0 else 0.0

        report: dict[str, Any] = {
            "provider_filter": provider or "all",
            "completeness": "partial",
            "total_baseline_count": total_count,
            "discovered": {
                "count": len(discovered_caps),
                "ratio": _safe_ratio(len(discovered_caps), total_count),
            },
            "callable": {
                "count": len(callable_caps),
                "ratio": _safe_ratio(len(callable_caps), total_count),
            },
            "platform_executable": {
                "count": len(platform_exec_caps),
                "ratio": _safe_ratio(len(platform_exec_caps), total_count),
            },
            "verified": {
                "count": len(verified_caps),
                "ratio": _safe_ratio(len(verified_caps), total_count),
            },
            "missing": {
                "count": len(missing_caps),
                "ratio": _safe_ratio(len(missing_caps), total_count),
                "items": missing_details,
            },
        }

        # 若未限定单一 provider，附带分引擎拆解
        if not provider:
            providers = sorted(list({c.engine.provider_id for c in all_caps}))
            report["by_provider"] = {p: self.audit_report(provider=p) for p in providers}

        return report

    def reconcile_baseline(
        self,
        baseline_files: Sequence[Path | str] | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """逐项核对契约基准文件与注册表条目（P5-01）。

        `audit_report` 的分母是**注册表条目**，不能直接证明基准文件中的每一项
        都已被登记。本方法读取 `contracts/baselines/*.json`，给出：

        - 基准有、注册表无的条目（必须为空）；
        - 注册表有、基准无的平台组合能力（显式列出，不静默混入基准）；
        - 所有 missing / blocked 条目及其限制说明（缺口必须有具体原因）。

        `complete` 只表示“基准对账完整且缺口均有说明”，**不表示**所有能力
        都 ready/verified。运行期缺失（如未安装的插件）仍保留在缺口中。
        """
        files = (
            [Path(p) for p in baseline_files]
            if baseline_files is not None
            else default_baseline_files()
        )
        errors: list[str] = []
        baseline_by_provider: dict[str, set[str]] = {}
        for path in files:
            try:
                _, descriptors = load_baseline_from_file(path)
            except Exception as exc:  # noqa: BLE001 - report, do not hide a broken baseline
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            for descriptor in descriptors:
                if provider and descriptor.engine.provider_id != provider:
                    continue
                baseline_by_provider.setdefault(descriptor.engine.provider_id, set()).add(
                    descriptor.capability_id
                )

        registry_caps = self.filter(provider=provider)
        registry_ids = {c.capability_id for c in registry_caps}
        baseline_ids: set[str] = set()
        for ids in baseline_by_provider.values():
            baseline_ids.update(ids)

        missing_from_registry = sorted(baseline_ids - registry_ids)
        platform_only = sorted(registry_ids - baseline_ids)

        gap_details: list[dict[str, Any]] = []
        for cap in registry_caps:
            if (
                cap.state.presence != Presence.missing
                and cap.state.availability == Availability.ready
            ):
                continue
            gap_details.append(
                {
                    "capability_id": cap.capability_id,
                    "name": cap.name,
                    "provider": cap.engine.provider_id,
                    "presence": cap.state.presence.value,
                    "availability": cap.state.availability.value,
                    "binding": cap.state.binding.value,
                    "limitations": list(cap.limitations),
                }
            )
        gap_details.sort(key=lambda item: item["capability_id"])
        unexplained = [item for item in gap_details if not item["limitations"]]

        by_provider: dict[str, Any] = {}
        provider_names = sorted(
            set(baseline_by_provider) | {c.engine.provider_id for c in registry_caps}
        )
        for name in provider_names:
            sub_caps = [c for c in registry_caps if c.engine.provider_id == name]
            sub_registry_ids = {c.capability_id for c in sub_caps}
            sub_baseline_ids = baseline_by_provider.get(name, set())
            sub_missing = sorted(sub_baseline_ids - sub_registry_ids)
            sub_platform = sorted(sub_registry_ids - sub_baseline_ids)
            sub_gaps = [item for item in gap_details if item["provider"] == name]
            sub_unexplained = [item for item in sub_gaps if not item["limitations"]]
            by_provider[name] = {
                "baseline_count": len(sub_baseline_ids),
                "registry_count": len(sub_registry_ids),
                "missing_from_registry": sub_missing,
                "platform_only": sub_platform,
                "gap_count": len(sub_gaps),
                "unexplained_gap_count": len(sub_unexplained),
            }

        return {
            "provider_filter": provider or "all",
            "baseline_files": [str(path) for path in files],
            "baseline_count": len(baseline_ids),
            "registry_count": len(registry_ids),
            "missing_from_registry": missing_from_registry,
            "platform_only": platform_only,
            "gap_count": len(gap_details),
            "missing_or_blocked": gap_details,
            "unexplained_gaps": unexplained,
            "errors": errors,
            "by_provider": by_provider,
            "complete": bool(files)
            and bool(baseline_ids)
            and not errors
            and not missing_from_registry
            and not unexplained,
        }

    def full_audit_report(
        self,
        baseline_files: Sequence[Path | str] | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """生成 P9-01 基准全量对账、分维度覆盖率与参数契约综合审计报告。"""
        from spectra_workbench.registry.parameter_audit import build_full_audit_report

        return build_full_audit_report(
            self,
            baseline_files=baseline_files,
            provider=provider,
        )

    def load_baseline_file(self, path: Path | str) -> None:
        """从 JSON 基准文件中载入并合并能力条目。"""
        _, descriptors = load_baseline_from_file(path)
        for d in descriptors:
            self.register(d)


def default_baseline_files() -> list[Path]:
    """返回仓库 `contracts/baselines/*.json`；打包环境无基准目录时返回空列表。"""
    root = Path(__file__).resolve().parents[3]
    folder = root / "contracts" / "baselines"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


_GLOBAL_REGISTRY: CapabilityRegistry | None = None


def get_default_registry() -> CapabilityRegistry:
    """获取预装 GDAL 3.13.3 与 Rasterio 1.5.2 官方基准的单例注册表。"""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is not None:
        return _GLOBAL_REGISTRY

    registry = CapabilityRegistry()

    # 1. 载入 GDAL 3.13.3 基准
    gdal_items = build_gdal_baseline()
    registry.register_batch(gdal_items, alias_map=GDAL_ALIASES)

    # 2. 载入 Rasterio 1.5.2 基准
    rasterio_items = build_rasterio_baseline()
    registry.register_batch(rasterio_items, alias_map=RASTERIO_ALIASES)

    # 3. 载入可选引擎扩展登记（P8-05：明确范围、依赖、入口与缺口，绝不作为已实现宣传）
    extension_items = build_optional_extension_descriptors()
    registry.register_batch(extension_items)

    _GLOBAL_REGISTRY = registry
    return registry


def export_standard_baselines(output_dir: Path | str) -> dict[str, Path]:
    """导出标准 GDAL 3.13.3 与 Rasterio 1.5.2 基准 JSON 文件至目标目录。"""
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # GDAL
    gdal_items = build_gdal_baseline()
    gdal_baseline = serialize_baseline(
        baseline_id=GDAL_BASELINE_ID,
        provider_id="gdal",
        upstream_version=GDAL_VERSION,
        target_version=GDAL_VERSION,
        documentation_url=GDAL_DOC_BASE,
        repository_url="https://github.com/OSGeo/gdal",
        upstream_tag=f"v{GDAL_VERSION}",
        release_date="2026-08-13",
        descriptors=gdal_items,
        completeness="partial",
        raw_source_sha256="66acdb60770795326e8482d7afff096d127d4f3fc99a75c1f5efe371eda9cf66",
        runtime_inventory_sha256="fbf9606d87007d91e59eb25b583a69b38f74b79e0024a582786dcd79fe2eb7ec",
        generator_version="1.0.0",
    )
    gdal_path = out / f"{GDAL_BASELINE_ID}.json"
    gdal_path.write_text(
        json.dumps(gdal_baseline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Rasterio
    rio_items = build_rasterio_baseline()
    rio_baseline = serialize_baseline(
        baseline_id=RASTERIO_BASELINE_ID,
        provider_id="rasterio",
        upstream_version=RASTERIO_VERSION,
        target_version=RASTERIO_VERSION,
        documentation_url=RASTERIO_DOC_BASE,
        repository_url="https://github.com/rasterio/rasterio",
        upstream_tag=RASTERIO_VERSION,
        release_date="2024-10-15",
        descriptors=rio_items,
        completeness="partial",
        raw_source_sha256="a2802613d7120fe1e4856f6bc42f5348bbab905feea541e204b6b60098f997cb",
        runtime_inventory_sha256="a2802613d7120fe1e4856f6bc42f5348bbab905feea541e204b6b60098f997cb",
        generator_version="1.0.0",
    )
    rio_path = out / f"{RASTERIO_BASELINE_ID}.json"
    rio_path.write_text(
        json.dumps(rio_baseline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {"gdal": gdal_path, "rasterio": rio_path}
