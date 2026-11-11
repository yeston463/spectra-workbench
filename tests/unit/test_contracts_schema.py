"""P1-01 契约对账验收：使用 jsonschema 校验领域模型与 contracts/*.schema.json 的一致性。

验证目标：
1. contracts/*.schema.json 符合 JSON Schema Draft 2020-12 规范。
2. contracts/examples/*.example.json 能通过对应的 schema 校验。
3. 领域模型（TaskSpec、CapabilityDescriptor、Envelope）导出的 JSON 能通过 schema 校验。
4. 契约示例能反序列化为领域模型实例，实现 100% 双向契约一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityEngineSpec,
    CapabilityKind,
    CapabilitySource,
    CapabilityState,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    ParameterSpec,
    PortSpec,
    Presence,
    Provenance,
    Requirement,
    TaskSpec,
    Validation,
)
from spectra_workbench.protocol.messages import Envelope, Kind

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def contracts_dir() -> Path:
    root = find_project_root()
    return root / "contracts"


@pytest.fixture(scope="module")
def task_schema(contracts_dir: Path) -> dict:
    return json.loads((contracts_dir / "task.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def capability_schema(contracts_dir: Path) -> dict:
    return json.loads((contracts_dir / "capability.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def message_schema(contracts_dir: Path) -> dict:
    return json.loads((contracts_dir / "message.schema.json").read_text(encoding="utf-8"))


def test_meta_schemas_valid(task_schema: dict, capability_schema: dict, message_schema: dict):
    """所有契约 schema 必须是合法的 JSON Schema Draft 2020-12 规范定义。"""
    for _name, schema in [
        ("task", task_schema),
        ("capability", capability_schema),
        ("message", message_schema),
    ]:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)


def test_contract_examples_validate_against_schemas(
    contracts_dir: Path, task_schema: dict, capability_schema: dict, message_schema: dict
):
    """契约模板样例必须通过 schema 校验。"""
    task_ex = json.loads(
        (contracts_dir / "examples" / "task.example.json").read_text(encoding="utf-8")
    )
    cap_ex = json.loads(
        (contracts_dir / "examples" / "capability.example.json").read_text(encoding="utf-8")
    )
    msg_ex = json.loads(
        (contracts_dir / "examples" / "message.example.json").read_text(encoding="utf-8")
    )

    jsonschema.validate(task_ex, task_schema)
    jsonschema.validate(cap_ex, capability_schema)
    jsonschema.validate(msg_ex, message_schema)


def test_task_example_deserializes_to_model(contracts_dir: Path):
    """task.example.json 能成功反序列化为 TaskSpec 领域模型。"""
    task_ex = json.loads(
        (contracts_dir / "examples" / "task.example.json").read_text(encoding="utf-8")
    )
    task = TaskSpec.model_validate(task_ex)
    assert task.template_only is True
    assert task.task_id == "example-convert-not-executable"
    assert task.engine.provider_id == "gdal"


def test_capability_example_deserializes_to_model(contracts_dir: Path):
    """capability.example.json 能成功反序列化为 CapabilityDescriptor 领域模型。"""
    cap_ex = json.loads(
        (contracts_dir / "examples" / "capability.example.json").read_text(encoding="utf-8")
    )
    cap = CapabilityDescriptor.model_validate(cap_ex)
    assert cap.template_only is True
    assert cap.capability_id == "gdal.algorithm.raster.convert"
    assert cap.kind == CapabilityKind.algorithm


def test_message_example_deserializes_to_model(contracts_dir: Path):
    """message.example.json 能成功反序列化为 Envelope 协议模型。"""
    msg_ex = json.loads(
        (contracts_dir / "examples" / "message.example.json").read_text(encoding="utf-8")
    )
    msg = Envelope.model_validate(msg_ex)
    assert msg.template_only is True
    assert msg.kind == Kind.progress
    assert msg.payload["ratio"] == 0.5


def test_task_model_serializes_and_passes_jsonschema(task_schema: dict):
    """由代码生成的 TaskSpec 实例，序列化后必须 100% 符合 task.schema.json 约束。"""
    task = TaskSpec(
        task_id="task-smoke-001",
        capability_id="gdal.algorithm.raster.convert",
        descriptor_revision="rev-sha256-abcdef",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="qgis-3.44.15", expected_version="3.13.3"
        ),
        project_root="C:/Users/test/project",
        workspace="C:/Users/test/project/work/tasks/task-smoke-001",
        inputs={
            "input": DatasetRef(
                dataset_id="ds-src-1",
                kind=DatasetKind.raster,
                uri="data/input.tif",
            )
        },
        parameters={"output_format": "GTiff"},
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri="outputs/converted.tif",
                driver="GTiff",
                creation_options={"TILED": "YES"},
                overwrite=False,
            )
        ],
        provenance=Provenance(origin="gui", notes="从 GUI 对话框提交"),
    )

    dumped = task.to_contract_dict()
    jsonschema.validate(dumped, task_schema)


def test_capability_model_serializes_and_passes_jsonschema(capability_schema: dict):
    """由代码生成的 CapabilityDescriptor 实例，序列化后必须 100% 符合 capability.schema.json 约束。"""
    cap = CapabilityDescriptor(
        capability_id="gdal.algorithm.raster.convert",
        descriptor_revision="rev-sha256-123456",
        kind=CapabilityKind.algorithm,
        name="栅格格式转换",
        native_name="raster convert",
        category=["栅格", "数据管理"],
        description="将栅格文件转换为目标格式并应用驱动选项",
        engine=CapabilityEngineSpec(
            provider_id="gdal",
            version_constraint=">=3.11,<4",
            runtime_id="qgis-3.44.15",
            resolved_version="3.13.3",
        ),
        source=CapabilitySource(
            documentation_url="https://gdal.org/en/stable/programs/gdal.html",
            baseline_id="gdal-3.13.3",
            upstream_version="3.13.3",
            snapshot_sha256="a" * 64,
            verified_date="2026-11-03",
        ),
        parameters=[
            ParameterSpec(
                key="output_format",
                native_name="output-format",
                label="输出格式",
                type="string",
                required=False,
                help="驱动短名，如 GTiff",
            )
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.raster,
                required=True,
                multiple=True,
                description="输入待转换影像",
            )
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="输出转换成果",
            )
        ],
        requirements=[
            Requirement(
                kind="driver",
                name="GTiff",
                reason="写入 GeoTIFF 成果",
            )
        ],
        state=CapabilityState(
            presence=Presence.present,
            binding=Binding.form,
            validation=Validation.smoke_passed,
            availability=Availability.ready,
        ),
        effects="new_dataset",
        cancellation="cooperative",
        limitations=["驱动选项由运行时环境决定"],
    )

    dumped = cap.to_contract_dict()
    jsonschema.validate(dumped, capability_schema)


def test_message_model_serializes_and_passes_jsonschema(message_schema: dict):
    """由代码生成的 Envelope 实例，序列化后必须 100% 符合 message.schema.json 约束。"""
    msg = Envelope(
        kind=Kind.progress,
        message_id="msg-progress-1",
        request_id="req-1",
        task_id="task-1",
        worker_id="sw-gdal-0",
        generation=1,
        payload={"ratio": 0.75, "phase": "processing", "message": "已完成 75%"},
    )

    dumped = msg.model_dump(mode="json")
    jsonschema.validate(dumped, message_schema)


def test_task_parameters_preserves_explicit_null_and_falsy_values(
    contracts_dir: Path, task_schema: dict
):
    """缺陷 1 验收：显式 null、0、false、空字符串、空列表、嵌套字典均被完整保留，不改变任务配置。"""
    task_ex = json.loads(
        (contracts_dir / "examples" / "task.example.json").read_text(encoding="utf-8")
    )
    test_params = {
        "src_nodata": None,
        "nested": {"optional_value": None, "sub_zero": 0, "active": False},
        "zero": 0,
        "flag_false": False,
        "empty_str": "",
        "empty_list": [],
        "empty_dict": {},
    }
    task_ex["parameters"] = test_params
    task_ex["configuration"] = {"timeout": None, "dry_run": False}
    task_ex["extensions"] = {"custom_tag": None, "priority_override": 0}

    task = TaskSpec.model_validate(task_ex)
    dumped = task.to_contract_dict()

    # 1. 契约校验必须完全通过
    jsonschema.validate(dumped, task_schema)

    # 2. 字段值必须严格无损保留
    assert dumped["parameters"] == test_params
    assert dumped["parameters"]["src_nodata"] is None
    assert dumped["parameters"]["nested"]["optional_value"] is None
    assert dumped["parameters"]["nested"]["active"] is False
    assert dumped["parameters"]["zero"] == 0
    assert dumped["parameters"]["flag_false"] is False
    assert dumped["parameters"]["empty_str"] == ""
    assert dumped["parameters"]["empty_list"] == []
    assert dumped["parameters"]["empty_dict"] == {}

    assert dumped["configuration"] == {"timeout": None, "dry_run": False}
    assert dumped["extensions"] == {"custom_tag": None, "priority_override": 0}

    # 3. 双向反序列化与等价性
    re_parsed = TaskSpec.model_validate(dumped)
    assert re_parsed.parameters == test_params
    assert re_parsed.configuration == {"timeout": None, "dry_run": False}
    assert re_parsed.extensions == {"custom_tag": None, "priority_override": 0}


@pytest.mark.parametrize(
    ("kind", "valid_payload"),
    [
        (Kind.hello, {"provider_id": "gdal", "worker_package_version": "1.0.0"}),
        (Kind.ready, {"runtime_id": "qgis-3.44.15", "features": ["raster", "vector"]}),
        (Kind.request, {"action": "gdal.translate", "arguments": {"format": "GTiff"}}),
        (Kind.accepted, {"attempt": 1}),
        (Kind.progress, {"ratio": 0.5, "phase": "processing", "message": "50%"}),
        (Kind.progress, {"ratio": None, "phase": "indeterminate"}),
        (Kind.log, {"level": "INFO", "message": "job started", "stream": "stdout"}),
        (Kind.log, {"level": "info", "message": "job progress"}),  # 大小写归一化
        (
            Kind.artifact,
            {
                "artifact_id": "a-1",
                "uri": "file:///tmp/out.tif",
                "kind": "raster",
                "commit_state": "committed",
            },
        ),
        (Kind.result, {"status": "succeeded", "artifact_ids": ["a-1"], "warnings": ["w1"]}),
        (
            Kind.error,
            {
                "code": "E_IO",
                "message": "disk full",
                "retryable": False,
                "details": {"free_bytes": 0},
            },
        ),
        (Kind.cancel, {"reason": "user clicked cancel"}),
        (Kind.heartbeat, {"monotonic_seconds": 123.45, "active_task_id": "task-001"}),
    ],
)
def test_envelope_accepts_valid_payload_for_all_kinds(
    kind: Kind, valid_payload: dict, message_schema: dict
):
    """缺陷 2 验收：所有合法消息均能通过 Envelope 模型与 JSON Schema 双重校验。"""
    env = Envelope(
        kind=kind,
        message_id="msg-test-valid",
        request_id="req-1",
        task_id="task-1",
        worker_id="sw-gdal-0",
        generation=1,
        payload=valid_payload,
    )
    dumped = env.model_dump(mode="json")
    jsonschema.validate(dumped, message_schema)


@pytest.mark.parametrize(
    ("kind", "invalid_payload", "error_keyword"),
    [
        (Kind.progress, {"ratio": 2.0, "phase": "test"}, "ratio"),
        (Kind.progress, {"ratio": -0.1, "phase": "test"}, "ratio"),
        (Kind.progress, {"ratio": "half", "phase": "test"}, "ratio"),
        (Kind.result, {"status": "definitely_not_a_valid_status", "artifact_ids": []}, "status"),
        (Kind.result, {"status": "succeeded", "artifact_ids": [""]}, "artifact_ids"),
        (Kind.result, {"status": "succeeded", "artifact_ids": "not_a_list"}, "artifact_ids"),
        (
            Kind.result,
            {"status": "succeeded", "artifact_ids": ["a1"], "warnings": [123]},
            "warnings",
        ),
        (Kind.log, {"level": "VERBOSE", "message": "bad level"}, "level"),
        (
            Kind.artifact,
            {"artifact_id": "", "uri": "u", "kind": "k", "commit_state": "committed"},
            "artifact_id",
        ),
        (
            Kind.artifact,
            {"artifact_id": "a", "uri": "u", "kind": "k", "commit_state": "bad_state"},
            "commit_state",
        ),
        (Kind.error, {"code": "E", "message": "m", "retryable": "not_a_bool"}, "retryable"),
        (Kind.error, {"code": "", "message": "m", "retryable": True}, "code"),
        (Kind.heartbeat, {"monotonic_seconds": -5.0}, "monotonic_seconds"),
        (Kind.heartbeat, {"monotonic_seconds": True}, "monotonic_seconds"),
        (Kind.accepted, {"attempt": 0}, "attempt"),
        (Kind.accepted, {"attempt": True}, "attempt"),
        (Kind.request, {"action": "", "arguments": {}}, "action"),
        (Kind.request, {"action": "act", "arguments": "not_a_dict"}, "arguments"),
        (Kind.ready, {"runtime_id": "", "features": []}, "runtime_id"),
        (Kind.ready, {"runtime_id": "r1", "features": [""]}, "features"),
        (Kind.hello, {"provider_id": "", "worker_package_version": "1.0"}, "provider_id"),
    ],
)
def test_envelope_rejects_invalid_payload_for_all_kinds(
    kind: Kind, invalid_payload: dict, error_keyword: str, message_schema: dict
):
    """缺陷 2 验收：所有非法消息模型均主动拒绝，且判定与 JSON Schema 完全一致。"""
    # 1. Envelope 模型必须拒绝并抛出 ValueError
    with pytest.raises(ValueError) as ei:
        Envelope(
            kind=kind,
            message_id="msg-test-invalid",
            request_id="req-1",
            task_id="task-1",
            worker_id="sw-gdal-0",
            generation=1,
            payload=invalid_payload,
        )
    assert error_keyword in str(ei.value)

    # 2. JSON Schema 判定也必须为拒绝
    raw_env = {
        "protocol_version": "1.0",
        "kind": kind.value,
        "message_id": "msg-test-invalid",
        "request_id": "req-1",
        "task_id": "task-1",
        "worker_id": "sw-gdal-0",
        "generation": 1,
        "payload": invalid_payload,
    }
    errors = list(jsonschema.Draft202012Validator(message_schema).iter_errors(raw_env))
    assert len(errors) > 0, (
        f"Schema 应当拒绝但却接受了非法 payload: kind={kind}, payload={invalid_payload}"
    )


@pytest.mark.parametrize(
    ("kind", "field_name", "base_payload", "valid_val", "invalid_type_val"),
    [
        (Kind.ready, "features", {"runtime_id": "r1"}, ["raster", "vector"], "not_a_list"),
        (Kind.progress, "message", {"ratio": 0.5, "phase": "p1"}, "working", 12345),
        (Kind.log, "stream", {"level": "INFO", "message": "msg"}, "stdout", "invalid_stream"),
        (
            Kind.result,
            "warnings",
            {"status": "succeeded", "artifact_ids": ["a1"]},
            ["warning_1"],
            "not_a_list",
        ),
        (
            Kind.error,
            "details",
            {"code": "E1", "message": "m1", "retryable": True},
            {"key": "val"},
            "not_a_dict",
        ),
        (Kind.cancel, "reason", {}, "user clicked cancel", 12345),
    ],
)
def test_message_optional_field_null_boundaries(
    kind: Kind,
    field_name: str,
    base_payload: dict,
    valid_val: Any,
    invalid_type_val: Any,
    message_schema: dict,
):
    """验证 6 个可选字段的四类边界：
    1. 省略：合法，序列化后不包含该字段，Schema 校验通过。
    2. 合法值：合法，序列化后包含该值，Schema 校验通过。
    3. 显式 null：Schema 不允许，Envelope 必须主动抛出 ValueError 拒绝。
    4. 错误类型：Schema 不允许，Envelope 必须主动抛出 ValueError 拒绝。
    """
    # 1. 省略测试
    env_omitted = Envelope(
        kind=kind,
        message_id="msg-omitted",
        request_id="req-1",
        task_id="task-1",
        worker_id="sw-gdal-0",
        generation=1,
        payload=base_payload.copy(),
    )
    dumped_omitted = env_omitted.model_dump(mode="json")
    assert field_name not in dumped_omitted["payload"], (
        f"{field_name} 被省略时不得输出到 payload 中"
    )
    jsonschema.validate(dumped_omitted, message_schema)

    # 2. 合法值测试
    payload_valid = base_payload.copy()
    payload_valid[field_name] = valid_val
    env_valid = Envelope(
        kind=kind,
        message_id="msg-valid",
        request_id="req-1",
        task_id="task-1",
        worker_id="sw-gdal-0",
        generation=1,
        payload=payload_valid,
    )
    dumped_valid = env_valid.model_dump(mode="json")
    assert dumped_valid["payload"][field_name] == valid_val
    jsonschema.validate(dumped_valid, message_schema)

    # 3. 显式 null 测试（Schema 拒绝，Envelope 模型必须主动拒绝）
    payload_null = base_payload.copy()
    payload_null[field_name] = None
    with pytest.raises(ValueError) as ei_null:
        Envelope(
            kind=kind,
            message_id="msg-null",
            request_id="req-1",
            task_id="task-1",
            worker_id="sw-gdal-0",
            generation=1,
            payload=payload_null,
        )
    assert field_name in str(ei_null.value)

    raw_null_env = {
        "protocol_version": "1.0",
        "kind": kind.value,
        "message_id": "msg-null",
        "request_id": "req-1",
        "task_id": "task-1",
        "worker_id": "sw-gdal-0",
        "generation": 1,
        "payload": payload_null,
    }
    null_errors = list(jsonschema.Draft202012Validator(message_schema).iter_errors(raw_null_env))
    assert len(null_errors) > 0, f"Schema 应当拒绝 {field_name}=null 但却接受了"

    # 4. 错误类型测试（Schema 拒绝，Envelope 模型必须主动拒绝）
    payload_invalid_type = base_payload.copy()
    payload_invalid_type[field_name] = invalid_type_val
    with pytest.raises(ValueError) as ei_type:
        Envelope(
            kind=kind,
            message_id="msg-type-err",
            request_id="req-1",
            task_id="task-1",
            worker_id="sw-gdal-0",
            generation=1,
            payload=payload_invalid_type,
        )
    assert field_name in str(ei_type.value)

    raw_type_env = {
        "protocol_version": "1.0",
        "kind": kind.value,
        "message_id": "msg-type-err",
        "request_id": "req-1",
        "task_id": "task-1",
        "worker_id": "sw-gdal-0",
        "generation": 1,
        "payload": payload_invalid_type,
    }
    type_errors = list(jsonschema.Draft202012Validator(message_schema).iter_errors(raw_type_env))
    assert len(type_errors) > 0, f"Schema 应当拒绝 {field_name}={invalid_type_val} 但却接受了"


def test_explicit_null_allowed_by_schema(message_schema: dict):
    """契约中明确允许显式 null 的字段（progress.ratio、heartbeat.active_task_id、request_id、task_id）正常通过。"""
    # 1. progress.ratio = None
    env_ratio_null = Envelope(
        kind=Kind.progress,
        message_id="m-ratio-null",
        request_id=None,
        task_id=None,
        worker_id="w-1",
        generation=0,
        payload={"ratio": None, "phase": "indeterminate"},
    )
    dumped_ratio = env_ratio_null.model_dump(mode="json")
    assert dumped_ratio["payload"]["ratio"] is None
    assert dumped_ratio["request_id"] is None
    assert dumped_ratio["task_id"] is None
    jsonschema.validate(dumped_ratio, message_schema)

    # 2. heartbeat.active_task_id = None
    env_hb_null = Envelope(
        kind=Kind.heartbeat,
        message_id="m-hb-null",
        request_id=None,
        task_id=None,
        worker_id="w-1",
        generation=0,
        payload={"monotonic_seconds": 12.34, "active_task_id": None},
    )
    dumped_hb = env_hb_null.model_dump(mode="json")
    assert dumped_hb["payload"]["active_task_id"] is None
    jsonschema.validate(dumped_hb, message_schema)
