"""P1-01 验收：合法输入通过；缺字段、错误类型、不支持版本被拒且能定位字段。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityEngineSpec,
    CapabilityKind,
    CapabilitySource,
    CapabilityState,
    CommitMode,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    GridSpec,
    LayerState,
    OutputPlan,
    Presence,
    Provenance,
    RasterMetadata,
    SpatialInfo,
    SpatialModel,
    StyleState,
    TaskResult,
    TaskSpec,
    TaskStatus,
    Validation,
    VectorMetadata,
    assert_transition,
)

pytestmark = pytest.mark.unit


def _engine():
    return EngineConstraint(
        provider_id="gdal", runtime_id="qgis-3.44.15", expected_version="3.13.3"
    )


def _task(**over):
    base = dict(
        task_id="t-1",
        capability_id="gdal.translate",
        descriptor_revision="r1",
        engine=_engine(),
        project_root="C:/work/项目",
        workspace="C:/work/项目/.spectra/tasks/t-1",
        inputs={
            "src": DatasetRef(dataset_id="d1", kind=DatasetKind.raster, uri="file:///C:/work/a.tif")
        },
        parameters={"otif": "float32"},
        outputs=[OutputPlan(key="out", kind=DatasetKind.raster, uri="file:///C:/work/out.tif")],
        provenance=Provenance(origin="gui"),
    )
    base.update(over)
    return TaskSpec(**base)


def test_valid_task_round_trip():
    t = _task()
    dumped = t.model_dump(mode="json")
    again = TaskSpec.model_validate(dumped)
    assert again == t
    assert dumped["protocol_version"] == "1.0"
    assert dumped["attempt"] == 1


def test_missing_required_field_locates_field():
    payload = _task().model_dump(mode="json")
    del payload["capability_id"]
    with pytest.raises(ValidationError) as ei:
        TaskSpec.model_validate(payload)
    locs = [tuple(e["loc"]) for e in ei.value.errors()]
    assert ("capability_id",) in locs, locs


def test_wrong_type_locates_nested_field():
    payload = _task().model_dump(mode="json")
    payload["attempt"] = "one"
    with pytest.raises(ValidationError) as ei:
        TaskSpec.model_validate(payload)
    assert ("attempt",) in [tuple(e["loc"]) for e in ei.value.errors()]


def test_unknown_field_rejected_not_silently_dropped():
    payload = _task().model_dump(mode="json")
    payload["mystery_option"] = 1
    with pytest.raises(ValidationError) as ei:
        TaskSpec.model_validate(payload)
    assert ("mystery_option",) in [tuple(e["loc"]) for e in ei.value.errors()]


def test_unsupported_protocol_version_rejected():
    with pytest.raises(ValidationError) as ei:
        _task(protocol_version="2.0")
    assert ("protocol_version",) in [tuple(e["loc"]) for e in ei.value.errors()]


def test_task_requires_at_least_one_output():
    with pytest.raises(ValidationError):
        _task(outputs=[])


def test_dataset_without_affine_is_legal():
    """仅 GCP / RPC / geolocation / 未配准像素影像都必须能建模。"""
    RasterMetadata(width=8, height=8, band_count=3, dtype="float32")  # 完全无空间信息
    RasterMetadata(
        width=8,
        height=8,
        band_count=3,
        dtype="float32",
        spatial=SpatialInfo(model=SpatialModel.gcp, gcp_count=12),
    )
    RasterMetadata(
        width=8,
        height=8,
        band_count=3,
        dtype="float32",
        spatial=SpatialInfo(model=SpatialModel.rpc, rpc_uri="file:///C:/work/rpc.rpc"),
    )


def test_model_affine_without_grid_is_rejected():
    with pytest.raises(ValidationError):
        SpatialInfo(model=SpatialModel.affine)


def test_rotated_affine_allowed_but_degenerate_rejected():
    GridSpec(width=4, height=4, affine=(1.0, 0.5, 0.0, 0.5, -1.0, 10.0), crs="EPSG:32650")
    with pytest.raises(ValidationError):
        GridSpec(width=4, height=4, affine=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0))


def test_nodata_zero_mask_alpha_are_distinct_facts():
    m = RasterMetadata(width=4, height=4, band_count=1, dtype="int16", nodata=0)
    assert m.nodata_is_zero is True
    m2 = RasterMetadata(
        width=4, height=4, band_count=1, dtype="int16", nodata=-9999, has_mask_band=True
    )
    assert m2.nodata_is_zero is False and m2.has_mask_band and not m2.has_alpha_band


def test_band_arrays_must_match_band_count():
    with pytest.raises(ValidationError):
        RasterMetadata(width=4, height=4, band_count=3, dtype="float32", wavelengths=[0.5, 0.6])


def test_scale_offset_formula_is_separate_from_raw_values():
    m = RasterMetadata(width=4, height=4, band_count=1, dtype="uint16", scale=1e-4, offset=0.0)
    assert m.physical_value_formula == "physical = raw * 0.0001 + 0.0"


def test_vector_unknown_feature_count_must_be_explicit():
    VectorMetadata(layer="l", feature_count=None, feature_count_is_known=False)
    with pytest.raises(ValidationError):
        VectorMetadata(layer="l", feature_count=None)


def test_style_stretch_source_must_be_labelled():
    with pytest.raises(ValidationError):
        StyleState(stretch_source="magic")


def test_capability_state_is_four_independent_dimensions():
    d = CapabilityDescriptor(
        capability_id="gdal.warp",
        descriptor_revision="r1",
        kind=CapabilityKind.algorithm,
        name="重投影",
        native_name="gdal warp",
        category=["raster"],
        engine=CapabilityEngineSpec(provider_id="gdal", version_constraint=">=3.11,<4"),
        source=CapabilitySource(
            documentation_url="https://gdal.org/en/stable/programs/gdal_warp.html",
            baseline_id="gdal-3.13.3",
            upstream_version="3.13.3",
            snapshot_sha256="0" * 64,
        ),
        state=CapabilityState(
            presence=Presence.present,
            binding=Binding.form,
            validation=Validation.not_run,
            availability=Availability.ready,
        ),
    )
    assert d.state.validation is Validation.not_run
    payload = d.model_dump(mode="json")
    # 有面板但未数值验证，不得被写成已验收
    bad = json.loads(json.dumps(payload))
    bad["state"]["validation"] = "numerical_passed"
    with pytest.raises(ValidationError):
        CapabilityDescriptor.model_validate(bad)
    missing_ready = json.loads(json.dumps(payload))
    missing_ready["state"] = {
        "presence": "missing",
        "binding": "none",
        "validation": "not_run",
        "availability": "ready",
    }
    with pytest.raises(ValidationError):
        CapabilityDescriptor.model_validate(missing_ready)


def test_retry_creates_new_attempt_and_keeps_original():
    t = _task()
    t2 = t.next_attempt()
    assert t.attempt == 1 and t2.attempt == 2
    assert t.task_id == t2.task_id


def test_result_terminal_consistency():
    TaskResult(task_id="t-1", attempt=1, status=TaskStatus.succeeded, outputs=[{"key": "out"}])
    with pytest.raises(ValidationError):
        TaskResult(task_id="t-1", attempt=1, status=TaskStatus.running)
    with pytest.raises(ValidationError):
        TaskResult(task_id="t-1", attempt=1, status=TaskStatus.succeeded, outputs=[])
    with pytest.raises(ValidationError):
        TaskResult(task_id="t-1", attempt=1, status=TaskStatus.failed)


@pytest.mark.parametrize(
    ("cur", "new", "ok"),
    [
        (TaskStatus.queued, TaskStatus.validating, True),
        (TaskStatus.running, TaskStatus.cancelling, True),
        (TaskStatus.cancelling, TaskStatus.cancelled, True),
        (TaskStatus.succeeded, TaskStatus.running, False),
        (TaskStatus.queued, TaskStatus.succeeded, False),
        (TaskStatus.running, TaskStatus.queued, False),
    ],
)
def test_state_machine_guards(cur, new, ok):
    if ok:
        assert_transition(cur, new)
    else:
        with pytest.raises(ValueError):
            assert_transition(cur, new)


def test_layer_state_carries_dataset_and_revision():
    ls = LayerState(
        layer_id="L1",
        dataset=DatasetRef(dataset_id="d1", kind=DatasetKind.raster, uri="file:///C:/x.tif"),
    )
    assert ls.revision == 1
    assert ls.style.opacity == 1.0
    assert CommitMode.new_dataset.value == "new_dataset"
