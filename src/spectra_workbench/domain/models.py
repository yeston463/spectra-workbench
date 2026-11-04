"""领域模型（P1-01）。

对齐 `contracts/task.schema.json`、`contracts/capability.schema.json` 的必填字段与
`additionalProperties: false` 约束。本模块**不得**导入 Qt、GDAL、Rasterio 或任何原生地理依赖。

三条容易被写错、因此在此显式建模的语义：

1. 数据集可以没有 affine（仅 GCP / RPC / geolocation / 未配准像素影像）。
2. 原始像元值、scale/offset 后的物理值、显示拉伸值是三个不同空间；NoData、mask、alpha
   与合法 0 值各是独立事实，不能用一个布尔混起来。
3. 失败/取消只终结当次 attempt；重试必须新建 attempt 并保留原记录。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = "1.0"
SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"1.0"})


class ContractModel(BaseModel):
    """所有跨进程契约的基类：禁止未知字段，避免 UI 静默丢参数。"""

    model_config = ConfigDict(extra="forbid")

    def to_contract_dict(self) -> dict[str, Any]:
        """导出符合 contracts/*.schema.json 的字典。

        保留开放字段（parameters、configuration、extensions 等）中的显式 null；
        非 nullable 可选字段由各具体子模型的序列化器精确过滤，不进行全局清洗。
        """
        return self.model_dump(mode="json")


class DatasetKind(StrEnum):
    raster = "raster"
    vector = "vector"
    table = "table"
    array = "array"
    scalar = "scalar"
    artifact = "artifact"
    any = "any"


class SpatialModel(StrEnum):
    affine = "affine"
    gcp = "gcp"
    rpc = "rpc"
    geolocation_array = "geolocation"
    none = "none"


class PixelConvention(StrEnum):
    center = "center"  # type: ignore[assignment]
    edge = "edge"


class CapabilityKind(StrEnum):
    algorithm = "algorithm"
    cli = "cli"
    function = "function"
    class_ = "class"
    method = "method"
    property_ = "property"
    enum = "enum"
    config = "config"
    driver = "driver"


class Presence(StrEnum):
    present = "present"
    missing = "missing"
    unknown = "unknown"


class Binding(StrEnum):
    none = "none"
    indexed = "indexed"
    script = "script"
    cli = "cli"
    form = "form"
    specialized = "specialized"


class Validation(StrEnum):
    not_run = "not_run"
    smoke_passed = "smoke_passed"
    numerical_passed = "numerical_passed"
    failed = "failed"
    not_applicable = "not_applicable"


class Availability(StrEnum):
    ready = "ready"
    missing_dependency = "missing_dependency"
    unsupported = "unsupported"
    blocked = "blocked"
    unknown = "unknown"


class TaskStatus(StrEnum):
    queued = "queued"
    validating = "validating"
    running = "running"
    cancelling = "cancelling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.interrupted}


_TERMINAL = {TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.interrupted}

_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.queued: {TaskStatus.validating, TaskStatus.cancelled, TaskStatus.interrupted},
    TaskStatus.validating: {TaskStatus.running, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.interrupted},
    TaskStatus.running: {TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelling, TaskStatus.interrupted},
    TaskStatus.cancelling: {TaskStatus.cancelled, TaskStatus.failed, TaskStatus.interrupted},
}


def assert_transition(current: TaskStatus, new: TaskStatus) -> None:
    """状态机守卫：终态不可再迁移，未登记的迁移直接报错。"""
    if current in _TERMINAL:
        raise ValueError(f"终态不可再迁移：{current.value} -> {new.value}")
    if new not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法状态迁移：{current.value} -> {new.value}")


class Priority(StrEnum):
    interactive = "interactive"
    normal = "normal"
    background = "background"


class CommitMode(StrEnum):
    new_dataset = "new_dataset"
    replace = "replace"  # type: ignore[assignment]
    append = "append"
    in_place = "in_place"
    remote_write = "remote_write"


class OverwritePolicy(StrEnum):
    fail_if_exists = "fail_if_exists"
    ask = "ask"
    replace = "replace"  # type: ignore[assignment]


class Fingerprint(ContractModel):
    """大文件默认不全文件哈希：size/mtime 快速指纹，强校验是显式选项。"""

    size_bytes: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    algorithm: str = "size_mtime"

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict):
            return {k: v for k, v in d.items() if v is not None}
        return d


class DatasetRef(ContractModel):
    dataset_id: str = Field(min_length=1)
    kind: DatasetKind
    uri: str = Field(min_length=1)
    selection: dict[str, Any] = Field(default_factory=dict)
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)
    auth_profile_id: str | None = None

    @field_validator("uri")
    @classmethod
    def _uri_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("数据集 URI 不得为空白")
        return v


class GridSpec(ContractModel):
    """网格规格。旋转 affine 也是一等公民，不假设永远与坐标轴对齐。"""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    affine: tuple[float, float, float, float, float, float] | None = None
    crs: str | None = None
    pixel_convention: PixelConvention = PixelConvention.center

    @model_validator(mode="after")
    def _reject_degenerate_affine(self):
        if self.affine is not None and all(v == 0.0 for v in self.affine):
            raise ValueError("全零退化 affine 非法")
        return self

    @property
    def has_rotation(self) -> bool:
        if self.affine is None:
            return False
        return self.affine[2] != 0.0 or self.affine[4] != 0.0


class SpatialInfo(ContractModel):
    model: SpatialModel
    crs: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    grid: GridSpec | None = None
    gcp_count: int | None = None
    rpc_uri: str | None = None
    has_rpc: bool = False
    has_geolocation_arrays: bool = False

    @model_validator(mode="after")
    def _model_consistency(self):
        if self.model is SpatialModel.affine and self.grid is None:
            raise ValueError("model=affine 时必须提供 grid")
        if self.model is SpatialModel.gcp and not self.gcp_count:
            raise ValueError("model=gcp 时必须提供 gcp_count > 0")
        if self.model is SpatialModel.rpc and not (self.has_rpc or self.rpc_uri):
            raise ValueError("model=rpc 时必须声明 has_rpc=True 或提供 rpc_uri")
        return self


class RasterMetadata(ContractModel):
    """语义严格分离：NoData、0 像元值、mask、alpha 是独立事实。"""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    band_count: int = Field(default=1, ge=1)
    dtype: str = Field(min_length=1)
    spatial: SpatialInfo | None = None
    driver_short_name: str | None = None
    nodata: float | None = None
    nodata_is_zero: bool = False
    has_mask_band: bool = False
    has_alpha_band: bool = False
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    wavelengths: list[float] | None = None

    @model_validator(mode="after")
    def _validate_metadata(self):
        if self.nodata == 0.0 or self.nodata == 0:
            self.nodata_is_zero = True
        if self.wavelengths is not None and len(self.wavelengths) != self.band_count:
            raise ValueError(f"波长数量 {len(self.wavelengths)} 与波段数 {self.band_count} 不匹配")
        return self

    @property
    def physical_value_formula(self) -> str:
        """物理值 = 原值 * scale + offset；未给 scale/offset 时退化为原值。"""
        s = 1.0 if self.scale is None else self.scale
        o = 0.0 if self.offset is None else self.offset
        return f"physical = raw * {s} + {o}"


class VectorField(ContractModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    width: int | None = None


class VectorMetadata(ContractModel):
    layer: str = Field(min_length=1)
    geometry_type: str | None = None
    crs: str | None = None
    fields: list[VectorField] = Field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    feature_count: int | None = None
    feature_count_is_known: bool = True

    @model_validator(mode="after")
    def _count_unknown_is_explicit(self):
        if self.feature_count is None and self.feature_count_is_known:
            raise ValueError("要素数未知时必须显式 feature_count_is_known=false，不得当作 0")
        return self


class StyleState(ContractModel):
    grayscale_band: int | None = None
    rgb: tuple[int, int, int] | None = None
    stretch: str = "min_max"
    stretch_source: str = "full_raster"
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("stretch_source")
    @classmethod
    def _source_known(cls, v: str):
        if v not in {"full_raster", "view_extent", "sample"}:
            raise ValueError(f"未知拉伸范围统计来源：{v}")
        return v


class LayerState(ContractModel):
    layer_id: str = Field(min_length=1)
    dataset: DatasetRef
    visible: bool = True
    style: StyleState = Field(default_factory=lambda: StyleState())
    revision: int = Field(default=1, ge=1)


class EngineConstraint(ContractModel):
    """Task 级别的 Engine 约束，对齐 contracts/task.schema.json。"""

    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    expected_version: str = Field(min_length=1)


class CapabilityEngineSpec(ContractModel):
    """Capability 级别的 Engine 规范，对齐 contracts/capability.schema.json。"""

    provider_id: str = Field(min_length=1)
    version_constraint: str = Field(min_length=1)
    runtime_id: str | None = None
    resolved_version: str | None = None


class CapabilitySource(ContractModel):
    """能力来源描述，对齐 contracts/capability.schema.json。"""

    documentation_url: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    upstream_version: str = Field(min_length=1)
    snapshot_sha256: str | None = None
    verified_date: str | None = None


class ParameterSpec(ContractModel):
    key: str = Field(min_length=1)
    native_name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: str = Field(min_length=1)
    required: bool
    repeatable: bool | None = None
    default: Any = None
    choices: list[Any] | None = None
    constraints: dict[str, Any] | None = None
    help: str | None = None
    advanced: bool | None = None
    extensions: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict):
            for k in ("repeatable", "default", "choices", "constraints", "help", "advanced", "extensions"):
                if d.get(k) is None:
                    d.pop(k, None)
        return d


class PortSpec(ContractModel):
    key: str = Field(min_length=1)
    data_type: DatasetKind
    required: bool
    multiple: bool | None = None
    description: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict):
            if d.get("multiple") is None:
                d.pop("multiple", None)
            if d.get("description") is None:
                d.pop("description", None)
        return d


class Requirement(ContractModel):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    constraint: str | None = None
    optional: bool | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict):
            if d.get("constraint") is None:
                d.pop("constraint", None)
            if d.get("optional") is None:
                d.pop("optional", None)
        return d


class Entrypoint(ContractModel):
    kind: str = Field(min_length=1)
    target: str = Field(min_length=1)
    available: bool
    notes: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict) and d.get("notes") is None:
            d.pop("notes", None)
        return d


class CapabilityState(ContractModel):
    """四个独立状态维度。不能用一个 supported 布尔混淆。"""

    presence: Presence
    binding: Binding
    validation: Validation
    availability: Availability


class Limitation(ContractModel):
    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    next_step: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict) and d.get("next_step") is None:
            d.pop("next_step", None)
        return d


class ValidationEvidence(ContractModel):
    case_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    report_uri: str | None = None
    runtime_id: str | None = None
    verified_at: str | None = None
    notes: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict):
            for k in ("report_uri", "runtime_id", "verified_at", "notes"):
                if d.get(k) is None:
                    d.pop(k, None)
        return d


class CapabilityDescriptor(ContractModel):
    schema_version: str = SCHEMA_VERSION
    template_only: bool = False
    capability_id: str = Field(min_length=1)
    descriptor_revision: str = Field(min_length=1)
    kind: CapabilityKind
    name: str = Field(min_length=1)
    native_name: str = Field(min_length=1)
    category: list[str] = Field(min_length=1)
    description: str = ""
    engine: CapabilityEngineSpec
    source: CapabilitySource
    parameters: list[ParameterSpec] = Field(default_factory=list)
    inputs: list[PortSpec] = Field(default_factory=list)
    outputs: list[PortSpec] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    entrypoints: list[Entrypoint] = Field(default_factory=list)
    state: CapabilityState
    effects: str = "new_dataset"
    cancellation: str = "cooperative"
    limitations: list[str] = Field(default_factory=list)
    validation_evidence: list[ValidationEvidence] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _schema_supported(cls, v: str):
        if v != SCHEMA_VERSION:
            raise ValueError(f"不支持的 capability schema_version：{v}（本实现支持 {SCHEMA_VERSION}）")
        return v

    @model_validator(mode="after")
    def _no_unverified_claim(self):
        if self.state.validation is Validation.numerical_passed and not self.validation_evidence:
            raise ValueError("validation=numerical_passed 必须给出 validation_evidence，不得空口宣称已验证")
        if self.state.availability is Availability.ready and self.state.presence is Presence.missing:
            raise ValueError("presence=missing 不能同时 availability=ready")
        return self


class ResourceRequest(ContractModel):
    max_cpu_threads: int = Field(default=1, ge=1)
    ram_limit_mib: int = Field(default=512, ge=64)
    priority: Priority = Priority.normal


class CancellationPolicy(ContractModel):
    cooperative_timeout_seconds: float = Field(default=5.0, ge=0)
    allow_process_termination: bool = True


class RetryPolicy(ContractModel):
    max_automatic_retries: int = Field(default=0, ge=0)
    external_side_effects: bool = False


class OutputPlan(ContractModel):
    key: str = Field(min_length=1)
    kind: DatasetKind
    uri: str = Field(min_length=1)
    driver: str | None = None
    creation_options: dict[str, Any] = Field(default_factory=dict)
    overwrite: bool = False
    commit_mode: CommitMode = CommitMode.new_dataset

    @field_validator("uri")
    @classmethod
    def _no_empty_path(cls, v: str):
        if not v.strip():
            raise ValueError("输出 uri 不得为空白")
        return v


class Provenance(ContractModel):
    origin: str = Field(min_length=1)
    workflow_id: str | None = None
    node_id: str | None = None
    notes: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        d = handler(self)
        if isinstance(d, dict) and d.get("notes") is None:
            d.pop("notes", None)
        return d


class TaskSpec(ContractModel):
    schema_version: str = SCHEMA_VERSION
    protocol_version: str = PROTOCOL_VERSION
    template_only: bool = False
    task_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    capability_id: str = Field(min_length=1)
    descriptor_revision: str = Field(min_length=1)
    engine: EngineConstraint
    project_root: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    inputs: dict[str, DatasetRef | list[DatasetRef]] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    outputs: list[OutputPlan] = Field(default_factory=list)
    resources: ResourceRequest = Field(
        default_factory=lambda: ResourceRequest(max_cpu_threads=1, ram_limit_mib=512, priority=Priority.normal)
    )
    configuration: dict[str, Any] = Field(default_factory=dict)
    cancellation: CancellationPolicy = Field(
        default_factory=lambda: CancellationPolicy(cooperative_timeout_seconds=5.0, allow_process_termination=True)
    )
    provenance: Provenance = Field(default_factory=lambda: Provenance(origin="gui"))
    retry: RetryPolicy = Field(default_factory=lambda: RetryPolicy(max_automatic_retries=0, external_side_effects=False))
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("protocol_version")
    @classmethod
    def _protocol_supported(cls, v: str):
        if v not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(f"不支持的 protocol_version：{v}（支持 {sorted(SUPPORTED_PROTOCOL_VERSIONS)}）")
        return v

    @model_validator(mode="after")
    def _at_least_one_output(self):
        if not self.outputs:
            raise ValueError("TaskSpec 必须声明至少一个输出计划（内存临时输出也要显式建模）")
        return self

    def next_attempt(self) -> TaskSpec:
        """重试新建 attempt，原记录保持不变。"""
        return self.model_copy(update={"attempt": self.attempt + 1})


class TaskResult(ContractModel):
    task_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    status: TaskStatus
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    engine_runtime: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _terminal_consistency(self):
        if not self.status.terminal:
            raise ValueError(f"TaskResult 只能是终态，收到 {self.status.value}")
        if self.status is TaskStatus.succeeded and not self.outputs:
            raise ValueError("succeeded 必须登记至少一个已提交输出；失败输出不得记为成功")
        if self.status in {TaskStatus.failed, TaskStatus.cancelled} and self.error is None:
            raise ValueError(f"{self.status.value} 必须给出 error 说明")
        return self
