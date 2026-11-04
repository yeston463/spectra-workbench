"""P3-05 ROI 与 P3-01 项目读写的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from spectra_workbench.domain.roi import ROI, ROIError, ROIShape
from spectra_workbench.project.model import (
    PROJECT_SCHEMA_VERSION,
    LayerRecord,
    ProjectError,
    ProjectSpec,
    ViewRecord,
    export_rois_geojson,
    find_missing,
    load_project,
    relink,
    resolve_source,
    save_project,
    store_source,
)
from spectra_workbench.rendering.display import DisplaySettings

pytestmark = pytest.mark.unit


def rect(c0=10, r0=20, c1=30, r1=45, **kw) -> ROI:
    return ROI(name="R", shape=ROIShape.rectangle, points=[(c0, r0), (c1, r1)], **kw)


def poly(points=((0, 0), (10, 0), (10, 10), (0, 10)), **kw) -> ROI:
    return ROI(name="P", shape=ROIShape.polygon, points=list(points), **kw)


# ---------------------------------------------------------------- ROI 形状


def test_rectangle_requires_two_points_and_non_degenerate():
    # 构造期领域错误由 pydantic 包装成 ValidationError，消息保留原文
    with pytest.raises(ValidationError, match="2 个对角点"):
        ROI(shape=ROIShape.rectangle, points=[(0, 0), (5, 5), (9, 9)])
    with pytest.raises(ValidationError, match="面积为 0"):
        ROI(shape=ROIShape.rectangle, points=[(3, 3), (3, 3)])


def test_polygon_requires_at_least_three_vertices():
    with pytest.raises(ValidationError, match="至少 3 个顶点"):
        ROI(shape=ROIShape.polygon, points=[(0, 0), (1, 1)])


def test_map_space_roi_requires_crs():
    with pytest.raises(ValidationError, match="crs_wkt"):
        ROI(coordinate_space="map", points=[(0, 0), (1, 1)])
    ok = ROI(coordinate_space="map", crs_wkt="PROJCS[...]", points=[(0, 0), (1, 1)])
    assert ok.coordinate_space == "map"


def test_bounds_and_window_round_up():
    r = rect(10.2, 20.7, 30.9, 45.1)
    assert r.bounds == (10.2, 20.7, 30.9, 45.1)
    x, y, w, h = r.rectangle_window()
    assert (x, y) == (10, 20)
    assert (w, h) == (21, 26), "窗口必须向上取整覆盖到最后一个被碰到的像元"


def test_window_never_zero_for_thin_selection():
    r = ROI(shape=ROIShape.rectangle, points=[(5.0, 5.0), (5.4, 5.4)])
    assert r.rectangle_window() == (5, 5, 1, 1)


def test_area_rectangle_and_polygon_shoelace():
    assert rect(0, 0, 4, 3).area_pixels() == 12.0
    assert poly(((0, 0), (4, 0), (4, 3), (0, 3))).area_pixels() == 12.0
    # 三角形 = 4*3/2
    assert poly(((0, 0), (4, 0), (0, 3))).area_pixels() == 6.0


def test_contains_inside_and_outside():
    r = rect(10, 10, 20, 20)
    assert r.contains(15, 15) and not r.contains(21, 15)
    # 凹多边形：用 L 形验证射线法不被包围盒骗过
    lshape = poly(((0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)))
    assert lshape.contains(2, 2)
    assert not lshape.contains(8, 8), "L 形缺口内的点不应算作命中"
    assert lshape.bounds == (0, 0, 10, 10)


def test_editing_returns_new_roi_and_leaves_original_untouched():
    r = rect()
    moved = r.moved(5, -2)
    assert moved.points == [(15, 18), (35, 43)]
    assert r.points == [(10, 20), (30, 45)], "编辑必须产生新对象，原 ROI 不变"
    assert moved.roi_id == r.roi_id

    p = poly()
    edited = p.with_vertex(1, 99, 99)
    assert edited.points[1] == (99, 99)
    assert p.points[1] == (10, 0)
    with pytest.raises(ROIError, match="越界"):
        p.with_vertex(9, 1, 1)
    with pytest.raises(ROIError, match="不能重合"):
        rect().with_vertex(1, 10, 20)


def test_geojson_ring_is_closed():
    def pix_to_map(c, r):
        return (500000 + 2 * c, 3200000 - 2 * r)

    g = rect(0, 0, 2, 4).to_geojson(pix_to_map)
    ring = g["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1], "GeoJSON 环必须闭合"
    assert len(ring) == 5
    gp = poly(((0, 0), (2, 0), (2, 2))).to_geojson(pix_to_map)
    assert len(gp["geometry"]["coordinates"][0]) == 4
    assert gp["properties"]["name"]


# ---------------------------------------------------------------- 项目路径


def test_store_source_relative_inside_absolute_outside(tmp_path: Path):
    root = tmp_path / "项目 dir"
    (root / "data").mkdir(parents=True)
    inside = root / "data" / "a.tif"
    inside.write_bytes(b"x")
    stored, is_rel = store_source(root, inside)
    assert is_rel and stored == "data/a.tif", stored
    assert resolve_source(root, stored, is_relative=True) == inside.resolve()

    outside = tmp_path / "outside.tif"
    outside.write_bytes(b"x")
    stored2, is_rel2 = store_source(root, outside)
    assert not is_rel2
    assert resolve_source(root, stored2, is_relative=False) == outside.resolve()


def test_chinese_and_space_paths_round_trip(tmp_path: Path):
    root = tmp_path / "中文 目录"
    (root / "子 目录").mkdir(parents=True)
    p = root / "子 目录" / "影像 1.tif"
    p.write_bytes(b"x")
    stored, is_rel = store_source(root, p)
    assert resolve_source(root, stored, is_relative=is_rel) == p.resolve()


def test_resolve_file_uri():
    got = resolve_source(Path("C:/x"), "file:///C:/x/a.tif")
    assert got.name == "a.tif"


# ---------------------------------------------------------------- 项目读写


def sample_spec(root: Path) -> ProjectSpec:
    (root / "data").mkdir(parents=True, exist_ok=True)
    a = root / "data" / "a.tif"
    b = root / "data" / "b.tif"
    for f in (a, b):
        f.write_bytes(b"x")
    sa, rela = store_source(root, a)
    sb, relb = store_source(root, b)
    return ProjectSpec(
        layers=[
            LayerRecord(layer_id="L1", name="A", source=sa, source_is_relative=rela, order=0,
                        display=DisplaySettings(mode="pseudo", pseudocolor="jet", opacity=0.6), visible=False),
            LayerRecord(layer_id="L2", name="B", source=sb, source_is_relative=relb, order=1),
        ],
        rois=[rect(), poly()],
        views=[ViewRecord(view_id="V1", center_col=100.5, center_row=200.25, scale=2.5, linked_views=["V2"]),
               ViewRecord(view_id="V2", center_col=1.0, center_row=2.0, scale=1.0)],
        active_layer_id="L2",
        active_view_id="V1",
        layout_b64="QUJD",
    )


def test_project_save_load_round_trip(tmp_path: Path):
    root = tmp_path / "proj root"
    root.mkdir()
    spec = sample_spec(root)
    file = root / "case.spectra.json"
    save_project(file, spec)
    back = load_project(file)
    assert back == spec
    assert back.ordered_layers()[0].layer_id == "L1"
    assert back.layers[0].display.pseudocolor == "jet"
    assert back.layers[0].visible is False
    assert len(back.rois) == 2 and back.rois[0].shape == ROIShape.rectangle
    assert back.views[0].linked_views == ["V2"]
    assert back.layout_b64 == "QUJD"


def test_save_is_atomic_and_leaves_no_temp(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    spec = sample_spec(root)
    file = root / "case.spectra.json"
    save_project(file, spec)
    leftovers = [p.name for p in root.iterdir() if p.name != "case.spectra.json" and p.is_file()]
    assert leftovers == [], leftovers
    # 覆盖已有项目也必须成功（替换而非追加）
    save_project(file, spec.model_copy(update={"notes": "第二次保存"}))
    assert load_project(file).notes == "第二次保存"


def test_save_rejects_wrong_extension(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    with pytest.raises(ProjectError, match="扩展名"):
        save_project(root / "case.txt", ProjectSpec())


def test_missing_schema_version_and_unknown_version_are_refused(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    bad1 = root / "n.json"
    bad1.write_text(json.dumps({"layers": []}), encoding="utf-8")
    with pytest.raises(ProjectError, match="schema_version"):
        load_project(bad1)

    bad2 = root / "old.json"
    bad2.write_text(json.dumps({"schema_version": "0.9", "layers": []}), encoding="utf-8")
    with pytest.raises(ProjectError, match="迁移"):
        load_project(bad2)

    bad3 = root / "broken.json"
    bad3.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="合法 JSON"):
        load_project(bad3)


def test_layer_order_must_be_contiguous(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    spec = sample_spec(root)
    broken = spec.model_copy(update={"layers": [
        spec.layers[0].model_copy(update={"order": 0}),
        spec.layers[1].model_copy(update={"order": 5}),
    ]})
    with pytest.raises(ValidationError, match="连续序号"):
        ProjectSpec.model_validate(broken.model_dump(mode="json"))

    dup = spec.model_copy(update={"layers": [spec.layers[0], spec.layers[0]]})
    with pytest.raises(ValidationError, match="重复"):
        ProjectSpec.model_validate(dup.model_dump(mode="json"))


def test_relative_flag_must_match_path_shape():
    with pytest.raises(ValidationError, match="绝对的"):
        LayerRecord(layer_id="L", name="n", source="C:/x/a.tif", source_is_relative=True, order=0)


def test_find_missing_and_relink(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    spec = sample_spec(root)
    # 删掉一个数据源，模拟项目被整体搬移/文件改名
    (root / "data" / "b.tif").unlink()
    missing = find_missing(root, spec)
    assert [m.layer_id for m in missing] == ["L2"]
    assert missing[0].tried.name == "b.tif"

    elsewhere = root / "moved"
    elsewhere.mkdir()
    target = elsewhere / "b_renamed.tif"
    target.write_bytes(b"x")
    fixed = relink(root, spec, {"L2": target})
    assert find_missing(root, fixed) == []
    assert fixed.layer_by_id("L2").source == "moved/b_renamed.tif"
    assert fixed.layer_by_id("L2").source_is_relative is True
    # 原 spec 不被就地修改
    assert spec.layer_by_id("L2").source == "data/b.tif"

    with pytest.raises(ProjectError, match="不存在"):
        relink(root, spec, {"NOPE": target})


def test_relink_outside_project_keeps_absolute(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    spec = sample_spec(root)
    outside = tmp_path / "outside.tif"
    outside.write_bytes(b"x")
    fixed = relink(root, spec, {"L1": outside})
    rec = fixed.layer_by_id("L1")
    assert rec.source_is_relative is False
    assert resolve_source(root, rec.source, is_relative=False) == outside.resolve()


def test_geojson_export_uses_pixel_to_map(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    spec = sample_spec(root)
    out = root / "rois.geojson"
    export_rois_geojson(spec, out, pixel_to_map=lambda c, r: (500000 + 2 * c, 3200000 - 2 * r))
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == 2
    ring = doc["features"][0]["geometry"]["coordinates"][0]
    # sample_spec 的矩形是 rect(10,20,30,45)
    assert ring[0] == [500000 + 2 * 10, 3200000 - 2 * 20], ring[0]
    assert ring[0] == ring[-1]
    assert len(ring) == 5


def test_project_version_constant_is_declared(tmp_path: Path):
    assert PROJECT_SCHEMA_VERSION == "1.0"



def test_rebase_project_relocates_relative_sources_correctly(tmp_path: Path):
    from spectra_workbench.project.model import rebase_project

    old_root = tmp_path / "old_proj"
    new_root = tmp_path / "new_proj"
    (old_root / "data").mkdir(parents=True)
    (new_root / "data").mkdir(parents=True)
    img = old_root / "data" / "a.tif"
    img.write_bytes(b"data")
    spec = ProjectSpec(
        layers=[LayerRecord(layer_id="L1", name="A", source="data/a.tif", source_is_relative=True, order=0)]
    )
    # 另存到 new_root（原文件在 old_root，不在 new_root 之下）-> 转为绝对路径保留对原数据的指向
    rebased = rebase_project(spec, old_root, new_root)
    assert rebased.layers[0].source_is_relative is False
    assert (
        resolve_source(new_root, rebased.layers[0].source, is_relative=rebased.layers[0].source_is_relative)
        == img.resolve()
    )

    # 若目标位于 new_root 之下 -> 自动转换为相对路径
    img_new = new_root / "data" / "b.tif"
    img_new.write_bytes(b"data")
    spec2 = ProjectSpec(
        layers=[
            LayerRecord(
                layer_id="L2",
                name="B",
                source=img_new.resolve().as_posix(),
                source_is_relative=False,
                order=0,
            )
        ]
    )
    rebased2 = rebase_project(spec2, old_root, new_root)
    assert rebased2.layers[0].source_is_relative is True
    assert rebased2.layers[0].source == "data/b.tif"


def test_find_missing_strictly_checks_project_dir_no_basename_hijack(tmp_path: Path):
    proj_dir = tmp_path / "proj"
    other_dir = tmp_path / "other"
    proj_dir.mkdir()
    other_dir.mkdir()
    # 异目录存在同名文件，不应被静默匹配
    (other_dir / "target.tif").write_bytes(b"wrong data")
    spec = ProjectSpec(
        layers=[
            LayerRecord(
                layer_id="L1",
                name="Target",
                source="data/target.tif",
                source_is_relative=True,
                order=0,
            )
        ]
    )
    missing = find_missing(proj_dir, spec)
    assert len(missing) == 1
    assert missing[0].tried == (proj_dir / "data" / "target.tif").resolve()
    assert not missing[0].tried.exists()
