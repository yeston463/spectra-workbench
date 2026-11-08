"""项目文件（P3-01）。

`.spectra.json` 只保存可移植配置：数据源引用（优先相对路径）、图层顺序与显隐、
显示参数、视图、ROI、界面布局字符串。不保存任何活的引擎句柄。

原子写：临时文件 + 同卷替换；写失败不留半个项目。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from spectra_workbench.domain.paths import (
    extract_local_path_from_vsi,
    is_vsi_or_remote,
    normalize_vsi_path,
)
from spectra_workbench.domain.roi import ROI
from spectra_workbench.rendering.display import DisplaySettings

PROJECT_SCHEMA_VERSION = "1.0"
PATH_ANCHOR_PROJECT_DIR = "project_dir"
PROJECT_EXTENSION = ".spectra.json"


class ProjectError(ValueError):
    pass


class LayerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source: str = Field(min_length=1)  # 相对项目根优先；也接受绝对路径与 file: URI
    source_is_relative: bool = True
    visible: bool = True
    order: int = Field(ge=0)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    provider_hint: str | None = None
    metadata_snapshot: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _relative_flag_matches(self) -> LayerRecord:
        looks_absolute = self.source.startswith(("/", "\\")) or ":" in self.source.split(os.sep)[0]
        if self.source_is_relative and looks_absolute:
            raise ProjectError(f"source_is_relative=true 但路径看起来是绝对的：{self.source}")
        return self


class ViewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_id: str = Field(min_length=1)
    center_col: float = 0.0
    center_row: float = 0.0
    scale: float = Field(1.0, gt=0)
    pixel_convention: str = "corner"
    view_crs_wkt: str | None = None
    linked_views: list[str] = Field(default_factory=list)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PROJECT_SCHEMA_VERSION
    project_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    path_anchor: str | None = PATH_ANCHOR_PROJECT_DIR
    layers: list[LayerRecord] = Field(default_factory=list)
    rois: list[ROI] = Field(default_factory=list)
    views: list[ViewRecord] = Field(default_factory=list)
    active_layer_id: str | None = None
    active_view_id: str | None = None
    layout_b64: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _version_supported(self) -> ProjectSpec:
        if self.schema_version != PROJECT_SCHEMA_VERSION:
            raise ProjectError(f"不支持的项目 schema_version：{self.schema_version}")
        ids = [layer.layer_id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ProjectError("图层 layer_id 重复")
        orders = [layer.order for layer in self.layers]
        if sorted(orders) != list(range(len(orders))):
            raise ProjectError(f"图层 order 必须是 0..N-1 的连续序号，收到 {sorted(orders)}")
        return self

    def ordered_layers(self) -> list[LayerRecord]:
        return sorted(self.layers, key=lambda layer: layer.order)

    def layer_by_id(self, layer_id: str) -> LayerRecord | None:
        for layer in self.layers:
            if layer.layer_id == layer_id:
                return layer
        return None


# ----------------------------------------------------------------------
# 路径处理：相对优先，缺失可重定位
# ----------------------------------------------------------------------


def store_source(project_root: Path, abs_path: Path | str) -> tuple[str, bool]:
    """返回 (存储路径, 是否相对)。项目文件内相对路径优先，项目外保留绝对/VSI路径。"""
    if is_vsi_or_remote(abs_path):
        return (normalize_vsi_path(abs_path), False)
    project_root = Path(project_root).resolve()
    abs_p = Path(abs_path).resolve()
    try:
        rel = abs_p.relative_to(project_root)
        return (rel.as_posix(), True)
    except ValueError:
        return (abs_p.as_posix(), False)


def resolve_source(project_root: Path, stored: str, *, is_relative: bool = True) -> Path | str:
    raw = stored.strip()
    if is_vsi_or_remote(raw):
        return normalize_vsi_path(raw)
    project_root = Path(project_root).resolve()
    if raw.startswith("file:"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        return Path(url2pathname(urlparse(raw).path)).resolve()
    cand = Path(raw)
    if cand.is_absolute():
        return cand.resolve()
    if is_relative:
        return (project_root / cand).resolve()
    return (project_root / cand).resolve()


def rebase_project(spec: ProjectSpec, old_root: Path, new_root: Path) -> ProjectSpec:
    """按新项目目录重算图层引用：

    原来相对 old_root 的路径先解析为原物理路径；
    若位于 new_root 之下则转为相对 new_root；否则转为绝对路径保留对原数据的指向。
    """
    old_root = Path(old_root).resolve()
    new_root = Path(new_root).resolve()
    if old_root == new_root:
        return spec.model_copy(update={"path_anchor": PATH_ANCHOR_PROJECT_DIR})
    new_layers = []
    for layer in spec.layers:
        abs_target = resolve_source(old_root, layer.source, is_relative=layer.source_is_relative)
        stored, is_rel = store_source(new_root, abs_target)
        new_layers.append(layer.model_copy(update={"source": stored, "source_is_relative": is_rel}))
    return spec.model_copy(update={"layers": new_layers, "path_anchor": PATH_ANCHOR_PROJECT_DIR})


def has_ambiguous_legacy_relative_sources(spec: ProjectSpec) -> bool:
    """若缺少 path_anchor 标记且存在相对路径图层，则存在路径基准歧义。"""
    if spec.path_anchor is not None:
        return False
    return any(layer.source_is_relative for layer in spec.layers)


@dataclass
class MissingSource:
    layer_id: str
    name: str
    stored: str
    tried: Path | str


def find_missing(project_root: Path, spec: ProjectSpec) -> list[MissingSource]:
    out = []
    for layer in spec.layers:
        path = resolve_source(project_root, layer.source, is_relative=layer.source_is_relative)
        local_p = extract_local_path_from_vsi(path)
        if local_p is not None and not local_p.exists():
            out.append(MissingSource(layer.layer_id, layer.name, layer.source, local_p))
    return out


def relink(project_root: Path, spec: ProjectSpec, mapping: dict[str, Path]) -> ProjectSpec:
    """把 layer_id -> 新路径 写回项目；重新按项目根决定相对/绝对存储。"""
    unknown = [key for key in mapping if spec.layer_by_id(key) is None]
    if unknown:
        raise ProjectError(f"重定位引用了不存在的图层：{unknown}")
    layers = []
    for layer in spec.layers:
        if layer.layer_id in mapping:
            stored, is_rel = store_source(project_root, mapping[layer.layer_id])
            layers.append(layer.model_copy(update={"source": stored, "source_is_relative": is_rel}))
        else:
            layers.append(layer)
    return spec.model_copy(update={"layers": layers})


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------


def save_project(path: Path | str, spec: ProjectSpec) -> Path:
    """原子保存：写临时文件 -> fsync -> 同卷替换。"""
    target = Path(path)
    if target.suffix != ".json" and not target.name.endswith(PROJECT_EXTENSION):
        raise ProjectError(f"项目文件应使用 {PROJECT_EXTENSION}（或 .json）扩展名，收到 {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if spec.path_anchor is None:
        spec = spec.model_copy(update={"path_anchor": PATH_ANCHOR_PROJECT_DIR})
    payload = spec.model_dump(mode="json")
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return target


def load_project(path: Path | str) -> ProjectSpec:
    """读取项目。未知版本与结构错误必须明确报错，不做静默宽容。

    **P7-01**：加载时先走 ``project.migrate.migrate_document``，
    legacy 0.9 项目无感迁移到 1.0；其他未知版本仍显式拒绝，不静默宽容。
    """
    src = Path(path)
    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ProjectError(f"项目文件不存在：{src}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectError(f"项目文件不是合法 JSON：{src}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ProjectError("项目文件顶层必须是对象")

    # P7-01：显式迁移（legacy 0.9 → 当前）；未知版本仍拒绝
    from spectra_workbench.project.migrate import migrate_document

    migrated = migrate_document(raw)

    try:
        spec = ProjectSpec.model_validate(migrated)
        if "path_anchor" not in raw:
            spec = spec.model_copy(update={"path_anchor": None})
        return spec
    except (ValidationError, ProjectError) as exc:
        raise ProjectError(f"项目文件校验失败：{exc}") from exc


def export_rois_geojson(spec: ProjectSpec, path: Path | str, pixel_to_map=None) -> Path:
    """导出 ROI。没有 pixel_to_map 时只导出像元坐标并在 crs 字段标注。"""
    target = Path(path)
    feats = []
    for roi in spec.rois:
        if roi.coordinate_space == "map":
            feats.append(roi.to_geojson(lambda c, r: (c, r)))
        elif pixel_to_map is not None:
            feats.append(roi.to_geojson(pixel_to_map))
        else:
            feats.append(roi.to_geojson(lambda c, r: (c, r)))
    doc = {
        "type": "FeatureCollection",
        "name": "rois",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:PixelCoordinate"}},
        "features": feats,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return target
