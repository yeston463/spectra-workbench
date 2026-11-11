"""插件 Manifest 契约与元数据模型（P8-04）。

定义插件元数据清单规范：
- ID、版本、provider_api_version 契约版本
- Worker 解释器/依赖规格 (WorkerSpec)
- 能力列表 (PluginCapabilitySpec) 转换为统一 CapabilityDescriptor
- 版本兼容性检测逻辑（provider_api_version 与 min/max platform version）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityEngineSpec,
    CapabilityKind,
    CapabilitySource,
    CapabilityState,
    DatasetKind,
    Entrypoint,
    ParameterSpec,
    PortSpec,
    Presence,
    Validation,
    ValidationEvidence,
)

PLATFORM_API_VERSION = "1.0"
PLATFORM_VERSION = "1.0.0"


class PluginStatus(StrEnum):
    """插件状态机。"""

    discovered = "discovered"
    enabled = "enabled"
    disabled = "disabled"
    incompatible = "incompatible"
    failed = "failed"


class WorkerSpec(BaseModel):
    """插件执行依赖与 Worker 运行环境要求。"""

    model_config = ConfigDict(extra="allow")

    engine: str = "plugin"
    python_version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class PluginCapabilitySpec(BaseModel):
    """插件暴露的单项能力规范。"""

    model_config = ConfigDict(extra="allow")

    capability_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = "algorithm"
    category: list[str] = Field(default_factory=lambda: ["plugin"])
    description: str = ""
    entrypoint: str = Field(min_length=1)  # 如 "zscore_op:run_zscore"
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)

    def to_descriptor(self, plugin_manifest: PluginManifest) -> CapabilityDescriptor:
        """转换为平台统一 CapabilityDescriptor 模型。"""
        # 端口转换
        in_ports: list[PortSpec] = []
        for inp in self.inputs:
            k = inp.get("key", "input")
            dt_str = inp.get("data_type", "raster")
            dt = (
                DatasetKind.raster
                if dt_str == "raster"
                else (DatasetKind.vector if dt_str == "vector" else DatasetKind.scalar)
            )
            in_ports.append(PortSpec(key=k, data_type=dt, required=bool(inp.get("required", True))))

        out_ports: list[PortSpec] = []
        for outp in self.outputs:
            k = outp.get("key", "output")
            dt_str = outp.get("data_type", "raster")
            dt = (
                DatasetKind.raster
                if dt_str == "raster"
                else (DatasetKind.vector if dt_str == "vector" else DatasetKind.scalar)
            )
            out_ports.append(
                PortSpec(key=k, data_type=dt, required=bool(outp.get("required", True)))
            )

        param_specs: list[ParameterSpec] = []
        for p in self.parameters:
            p_key = str(p.get("key") or p.get("name") or "param")
            param_specs.append(
                ParameterSpec(
                    key=p_key,
                    native_name=str(p.get("native_name") or p_key),
                    label=str(p.get("label") or p_key),
                    type=str(p.get("type") or "string"),
                    required=bool(p.get("required", False)),
                    default=p.get("default"),
                    help=str(p.get("help") or p.get("description") or ""),
                )
            )

        kind_enum = CapabilityKind.algorithm
        if self.kind == "function":
            kind_enum = CapabilityKind.function
        elif self.kind == "cli":
            kind_enum = CapabilityKind.cli

        return CapabilityDescriptor(
            capability_id=self.capability_id,
            descriptor_revision=plugin_manifest.version,
            kind=kind_enum,
            name=self.name,
            native_name=f"{plugin_manifest.id}.{self.capability_id}",
            category=list(self.category),
            description=self.description or f"Provided by plugin {plugin_manifest.name}",
            engine=CapabilityEngineSpec(
                provider_id="plugin",
                version_constraint=f">={plugin_manifest.version}",
            ),
            source=CapabilitySource(
                documentation_url=plugin_manifest.documentation
                or "https://spectra-workbench.local",
                baseline_id=f"plugin:{plugin_manifest.id}",
                upstream_version=plugin_manifest.version,
            ),
            parameters=param_specs,
            inputs=in_ports,
            outputs=out_ports,
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.smoke_passed,
                availability=Availability.ready,
            ),
            entrypoints=[Entrypoint(kind="api", target=self.entrypoint, available=True)],
            validation_evidence=[
                ValidationEvidence(
                    case_id=f"plugin:{plugin_manifest.id}:{self.capability_id}",
                    status="verified",
                    notes="Plugin manifest capability declaration",
                )
            ],
        )


class PluginManifest(BaseModel):
    """插件 Manifest 规范（plugin.json）。"""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    provider_api_version: str = PLATFORM_API_VERSION
    min_platform_version: str = "0.1.0"
    max_platform_version: str | None = None
    description: str = ""
    author: str = ""
    entry_point: str = ""
    worker_spec: WorkerSpec = Field(default_factory=WorkerSpec)
    capabilities: list[PluginCapabilitySpec] = Field(default_factory=list)
    ui_contributions: dict[str, Any] = Field(default_factory=dict)
    documentation: str = ""

    def check_compatibility(
        self,
        current_api_version: str = PLATFORM_API_VERSION,
        current_platform_version: str = PLATFORM_VERSION,
    ) -> tuple[bool, str | None]:
        """验证插件与当前平台的 API 契约与版本兼容性。"""
        # 1. 检查 provider_api_version 主版本一致性
        req_major = self.provider_api_version.split(".")[0]
        cur_major = current_api_version.split(".")[0]
        if req_major != cur_major:
            return (
                False,
                f"Plugin {self.id} requires provider_api_version {self.provider_api_version}, but platform supports {current_api_version}",
            )

        # 2. 检查 min_platform_version
        try:
            req_min_parts = [int(p) for p in self.min_platform_version.split(".")[:3]]
            cur_parts = [int(p) for p in current_platform_version.split(".")[:3]]
            if cur_parts < req_min_parts:
                return (
                    False,
                    f"Plugin {self.id} requires minimum platform version {self.min_platform_version}, but current version is {current_platform_version}",
                )
        except Exception:
            pass

        # 3. 检查 max_platform_version
        if self.max_platform_version:
            try:
                req_max_parts = [int(p) for p in self.max_platform_version.split(".")[:3]]
                cur_parts = [int(p) for p in current_platform_version.split(".")[:3]]
                if cur_parts > req_max_parts:
                    return (
                        False,
                        f"Plugin {self.id} specifies maximum platform version {self.max_platform_version}, but current version is {current_platform_version}",
                    )
            except Exception:
                pass

        return True, None


@dataclass
class PluginInfo:
    """插件运行时状态记录。"""

    manifest: PluginManifest
    status: PluginStatus = PluginStatus.discovered
    install_path: Path | None = None
    incompatibility_reason: str | None = None
    consecutive_failures: int = 0
    installed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "version": self.manifest.version,
            "status": self.status.value,
            "install_path": str(self.install_path) if self.install_path else None,
            "incompatibility_reason": self.incompatibility_reason,
            "consecutive_failures": self.consecutive_failures,
            "installed_at": self.installed_at,
            "capabilities": [c.capability_id for c in self.manifest.capabilities],
        }
