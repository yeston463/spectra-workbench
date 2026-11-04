"""P3 项目持久化与相对路径基准回归测试。

覆盖：
1. 程序根与用户项目目录不同。
2. 项目目录内的数据保存为相对引用。
3. 整体复制/搬移项目包，原位能正确打开并完成真实栅格探测。
4. 另存为不同目录（通过 mock 文件对话框真实驱动 save_as=True），相对引用重算且指向原数据。
5. 保存事务边界：Save As 失败不污染内存状态，原项目重试保存指向不变。
6. 打开含缺失图层项目，保存后缺失图层记录、顺序、样式、ROI 不丢失，解析出的源路径一致。
7. 部分重新关联，未关联的保持缺失，已关联的正常，保存不丢失任何记录。
8. 异目录同名文件不发生静默劫持。
9. 旧项目路径基准歧义明确提示为缺失待重新关联，禁止静默匹配。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROI, ROIShape
from spectra_workbench.project.model import (
    LayerRecord,
    ProjectSpec,
    load_project,
    resolve_source,
    save_project,
)
from spectra_workbench.rendering.display import DisplaySettings
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

pytestmark = [pytest.mark.integration, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


def wait_layers(win: MainWindow, qapp: QApplication, count: int = 1, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"等待图层加载超时（期望 {count}，实际 {len(win.layer_panel.layers)}）；"
        f"状态栏：{win.status_bar.currentMessage()}"
    )


def test_runtime_root_separated_from_user_project_and_relative_persistence(
    qapp: QApplication, tmp_path: Path
) -> None:
    app_root = tmp_path / "runtime_workbench_app"
    app_root.mkdir()
    user_proj = tmp_path / "user_case_A"
    data_dir = user_proj / "data"
    data_dir.mkdir(parents=True)
    data_file = data_dir / "synthetic.dat"
    data_file.write_bytes(b"synthetic payload 1")

    win = MainWindow(project_root=app_root)
    layer = LayerItem("synthetic", data_file, {"width": 10, "height": 10, "count": 1})
    win.layer_panel.add_layer(layer, select=False)

    proj_file = user_proj / "case.spectra.json"
    win.project_path = proj_file
    assert win.save_project_action() is True
    win.close()
    qapp.processEvents()

    spec = load_project(proj_file)
    assert len(spec.layers) == 1
    assert spec.layers[0].source == "data/synthetic.dat"
    assert spec.layers[0].source_is_relative is True


def test_moving_project_bundle_restores_and_probes_valid_raster(
    qapp: QApplication, tmp_path: Path
) -> None:
    """整体复制搬移项目包后，原位置删除；新位置打开并等待 worker 真实探测栅格成功。"""
    repo_fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "data"
    ramp_dat = repo_fixtures / "a01_ramp_16x16.dat"
    ramp_hdr = repo_fixtures / "a01_ramp_16x16.hdr"
    assert ramp_dat.exists() and ramp_hdr.exists(), "必须存在有效 ENVI 栅格样例"

    app_root = tmp_path / "runtime_app"
    app_root.mkdir()
    bundle_a = tmp_path / "bundle_A"
    (bundle_a / "data").mkdir(parents=True)
    shutil.copyfile(ramp_dat, bundle_a / "data" / "ramp.dat")
    shutil.copyfile(ramp_hdr, bundle_a / "data" / "ramp.hdr")
    proj_a = bundle_a / "project.spectra.json"

    win = MainWindow(project_root=app_root)
    win.open_dataset(bundle_a / "data" / "ramp.dat")
    wait_layers(win, qapp, 1)

    win.project_path = proj_a
    assert win.save_project_action() is True
    win.close()
    qapp.processEvents()

    # 整体搬移到 bundle_B，并彻底删除原 bundle_A
    bundle_b = tmp_path / "bundle_B"
    shutil.copytree(bundle_a, bundle_b)
    shutil.rmtree(bundle_a)

    win2 = MainWindow(project_root=app_root)
    assert win2.open_project(bundle_b / "project.spectra.json") is True
    wait_layers(win2, qapp, 1)

    assert len(win2.missing_sources) == 0
    assert len(win2.layer_panel.layers) == 1
    loaded = win2.layer_panel.layers[0]
    assert loaded.width == 16 and loaded.height == 16
    assert win2.save_project_action() is True
    win2.close()
    qapp.processEvents()

    after = load_project(bundle_b / "project.spectra.json")
    assert len(after.layers) == 1
    assert after.layers[0].source == "data/ramp.dat"
    assert after.layers[0].source_is_relative is True


def test_save_as_different_dir_via_dialog_rebases_correctly(
    qapp: QApplication, tmp_path: Path
) -> None:
    """通过 mock 文件对话框驱动真实 save_project_action(save_as=True)，验证重算路径。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir()
    orig_proj_dir = tmp_path / "orig_proj"
    (orig_proj_dir / "data").mkdir(parents=True)
    data_file = orig_proj_dir / "data" / "scene.dat"
    data_file.write_bytes(b"scene data")

    win = MainWindow(project_root=app_root)
    layer = LayerItem("scene", data_file, {"width": 16, "height": 16, "count": 1})
    win.layer_panel.add_layer(layer, select=False)
    orig_proj = orig_proj_dir / "orig.spectra.json"
    win.project_path = orig_proj
    assert win.save_project_action() is True

    # 另存为到不同目录 dest_dir
    dest_dir = tmp_path / "other_place" / "sub"
    dest_dir.mkdir(parents=True)
    new_proj = dest_dir / "saved_as.spectra.json"

    with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(new_proj), "")):
        assert win.save_project_action(save_as=True) is True
    assert win.project_path == new_proj
    win.close()
    qapp.processEvents()

    spec_new = load_project(new_proj)
    assert len(spec_new.layers) == 1
    resolved = resolve_source(dest_dir, spec_new.layers[0].source, is_relative=spec_new.layers[0].source_is_relative)
    assert resolved == data_file.resolve()


def test_save_as_failure_does_not_corrupt_memory_and_retry_succeeds(
    qapp: QApplication, tmp_path: Path
) -> None:
    """保存事务边界：另存为写磁盘失败时，内存快照不被提前修改，原项目重试保存无污染。"""
    app_root = tmp_path / "runtime_app"
    user_proj = tmp_path / "case_txn"
    user_proj.mkdir()
    original_proj = user_proj / "original.spectra.json"
    expected_missing = user_proj / "data" / "missing.dat"
    spec = ProjectSpec(
        layers=[
            LayerRecord(
                layer_id="missing_layer",
                name="Missing",
                source="data/missing.dat",
                source_is_relative=True,
                order=0,
            )
        ]
    )
    save_project(original_proj, spec)

    win = MainWindow(project_root=app_root)
    assert win.open_project(original_proj) is True
    assert win.save_project_action() is True

    # 另存为到父目录，但注入 save_project 的 OSError 模拟写权限失败
    destination = tmp_path / "save_as.spectra.json"
    with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
         patch("spectra_workbench.ui.main_window.save_project", side_effect=OSError("injected write failure")):
        result = win.save_project_action(save_as=True)
    assert result is False
    # 事务未完成：project_path 与内存图层记录必须保持原样
    assert win.project_path == original_proj
    assert win._project_records["missing_layer"].source == "data/missing.dat"

    # 再次保存原项目：必须仍旧解析到原本正确的物理目标
    assert win.save_project_action() is True
    saved_rec = load_project(original_proj).layers[0]
    actual = resolve_source(user_proj, saved_rec.source, is_relative=saved_rec.source_is_relative)
    assert actual == expected_missing.resolve()
    win.close()
    qapp.processEvents()


def test_missing_layer_preserved_on_save_and_save_as(qapp: QApplication, tmp_path: Path) -> None:
    """验证 loaded / missing 混合项目在保存与另存为前后，所有图层解析出的绝对路径保持不变。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir()
    proj_dir = tmp_path / "proj_with_missing"
    (proj_dir / "data").mkdir(parents=True)
    exist_file = proj_dir / "data" / "exist.dat"
    exist_file.write_bytes(b"existing data")
    expected_missing_target = (proj_dir / "data" / "missing.dat").resolve()

    roi1 = ROI(name="R1", shape=ROIShape.rectangle, points=[(10, 10), (20, 20)], attributes={"layer_id": "L1"})
    roi2 = ROI(
        name="R2",
        shape=ROIShape.polygon,
        points=[(0, 0), (10, 0), (5, 5)],
        attributes={"layer_id": "L2"},
    )
    spec = ProjectSpec(
        layers=[
            LayerRecord(
                layer_id="L1",
                name="ExistLayer",
                source="data/exist.dat",
                source_is_relative=True,
                order=0,
            ),
            LayerRecord(
                layer_id="L2",
                name="MissingLayer",
                source="data/missing.dat",
                source_is_relative=True,
                order=1,
                display=DisplaySettings(mode="gray", gray_band=2, opacity=0.75),
            ),
        ],
        rois=[roi1, roi2],
    )
    proj_file = proj_dir / "case.spectra.json"
    save_project(proj_file, spec)

    win = MainWindow(project_root=app_root)
    assert win.open_project(proj_file) is True
    assert len(win.missing_sources) == 1
    assert win.missing_sources[0].layer_id == "L2"
    assert not win.layer_panel.grp_missing.isHidden()

    # 打开后不重新关联直接保存 -> 缺失图层记录与 ROI 不得丢失，且源路径解析一致
    assert win.save_project_action() is True
    saved = load_project(proj_file)
    assert len(saved.layers) == 2
    assert [ly.layer_id for ly in saved.layers] == ["L1", "L2"]
    assert saved.layers[1].source == "data/missing.dat"
    assert saved.layers[1].display.opacity == 0.75
    assert len(saved.rois) == 2
    assert {r.attributes.get("layer_id") for r in saved.rois} == {"L1", "L2"}

    # 验证物理目标解析
    resolved_l1 = resolve_source(proj_dir, saved.layers[0].source, is_relative=saved.layers[0].source_is_relative)
    resolved_l2 = resolve_source(proj_dir, saved.layers[1].source, is_relative=saved.layers[1].source_is_relative)
    assert resolved_l1 == exist_file.resolve()
    assert resolved_l2 == expected_missing_target

    # 另存为到新目录 -> 缺失图层与 ROI 同样完整保留，且解析出的源依然指向原物理位置
    as_dir = tmp_path / "as_dir"
    as_dir.mkdir()
    as_proj = as_dir / "as.spectra.json"
    with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(as_proj), "")):
        assert win.save_project_action(save_as=True) is True
    saved_as = load_project(as_proj)
    assert len(saved_as.layers) == 2
    assert [ly.layer_id for ly in saved_as.layers] == ["L1", "L2"]
    assert len(saved_as.rois) == 2

    as_resolved_l1 = resolve_source(as_dir, saved_as.layers[0].source, is_relative=saved_as.layers[0].source_is_relative)
    as_resolved_l2 = resolve_source(as_dir, saved_as.layers[1].source, is_relative=saved_as.layers[1].source_is_relative)
    assert as_resolved_l1 == exist_file.resolve()
    assert as_resolved_l2 == expected_missing_target
    win.close()
    qapp.processEvents()


def test_partial_relink_preserves_both_relinked_and_unrelinked(
    qapp: QApplication, tmp_path: Path
) -> None:
    app_root = tmp_path / "runtime_app"
    app_root.mkdir()
    proj_dir = tmp_path / "proj_partial"
    (proj_dir / "data").mkdir(parents=True)
    spec = ProjectSpec(
        layers=[
            LayerRecord(layer_id="M1", name="Missing1", source="data/m1.dat", source_is_relative=True, order=0),
            LayerRecord(layer_id="M2", name="Missing2", source="data/m2.dat", source_is_relative=True, order=1),
        ]
    )
    proj_file = proj_dir / "case.spectra.json"
    save_project(proj_file, spec)

    win = MainWindow(project_root=app_root)
    assert win.open_project(proj_file) is True
    assert len(win.missing_sources) == 2

    # 创建新的 m1 实体并执行部分重关联
    new_m1 = proj_dir / "data" / "relinked_m1.dat"
    new_m1.write_bytes(b"relinked data")

    assert win.relink_layer("M1", new_m1) is True
    assert len(win.missing_sources) == 1
    assert win.missing_sources[0].layer_id == "M2"

    # 保存项目 -> 已关联的 M1 更新为新源，未关联的 M2 保持缺失记录，不丢任何一项
    assert win.save_project_action() is True
    saved = load_project(proj_file)
    assert len(saved.layers) == 2
    assert saved.layers[0].layer_id == "M1"
    assert saved.layers[0].source == "data/relinked_m1.dat"
    assert saved.layers[1].layer_id == "M2"
    assert saved.layers[1].source == "data/m2.dat"
    win.close()
    qapp.processEvents()


def test_explicit_removal_removes_missing_layer(qapp: QApplication, tmp_path: Path) -> None:
    app_root = tmp_path / "runtime_app"
    app_root.mkdir()
    proj_dir = tmp_path / "proj_remove"
    (proj_dir / "data").mkdir(parents=True)
    (proj_dir / "data" / "keep.dat").write_bytes(b"keep")

    spec = ProjectSpec(
        layers=[
            LayerRecord(layer_id="K1", name="Keep", source="data/keep.dat", source_is_relative=True, order=0),
            LayerRecord(layer_id="M1", name="ToDrop", source="data/drop.dat", source_is_relative=True, order=1),
        ]
    )
    proj_file = proj_dir / "case.spectra.json"
    save_project(proj_file, spec)

    win = MainWindow(project_root=app_root)
    assert win.open_project(proj_file) is True
    assert len(win.missing_sources) == 1

    # 用户明确点击“移除记录”
    win._on_missing_layer_removed("M1")
    assert len(win.missing_sources) == 0
    assert win.layer_panel.grp_missing.isHidden()

    assert win.save_project_action() is True
    saved = load_project(proj_file)
    assert len(saved.layers) == 1
    assert saved.layers[0].layer_id == "K1"
    win.close()
    qapp.processEvents()


def test_no_silent_hijacking_of_same_basename_in_other_dirs(
    qapp: QApplication, tmp_path: Path
) -> None:
    app_root = tmp_path / "workbench_app_root"
    (app_root / "data").mkdir(parents=True)
    (app_root / "data" / "target.dat").write_bytes(b"hijack attempt from app root")

    other_dir = tmp_path / "other_random_place"
    (other_dir / "data").mkdir(parents=True)
    (other_dir / "data" / "target.dat").write_bytes(b"hijack attempt from other dir")

    user_proj = tmp_path / "user_proj"
    user_proj.mkdir()
    proj_file = user_proj / "isolated.spectra.json"

    # 引用 data/target.dat，但 user_proj 目录下并不存在此文件
    spec = ProjectSpec(
        layers=[
            LayerRecord(
                layer_id="T1",
                name="StrictTarget",
                source="data/target.dat",
                source_is_relative=True,
                order=0,
            )
        ]
    )
    save_project(proj_file, spec)

    win = MainWindow(project_root=app_root)
    assert win.open_project(proj_file) is True
    # 必须报告缺失，严禁静默匹配 app_root 或其它目录下的同名 target.dat
    assert len(win.missing_sources) == 1
    assert win.missing_sources[0].tried == (user_proj / "data" / "target.dat").resolve()
    assert len(win.layer_panel.layers) == 0
    win.close()
    qapp.processEvents()


def test_old_project_missing_path_anchor_prompts_relink_no_silent_guess(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧项目路径基准歧义：两处同名不同内容时禁止静默派发，返回待确认状态并允许按用户确认派发。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    orig_file = app_root / "legacy.tif"
    orig_file.write_bytes(b"original data in app root")

    proj_dir = tmp_path / "old_proj"
    proj_dir.mkdir()
    diff_file = proj_dir / "legacy.tif"
    diff_file.write_bytes(b"different same-named data in project dir")

    raw_json = {
        "schema_version": "1.0",
        "project_id": "legacy_01",
        "layers": [
            {
                "layer_id": "L_old",
                "name": "LegacyLayer",
                "source": "legacy.tif",
                "source_is_relative": True,
                "visible": True,
                "order": 0,
            }
        ],
    }
    proj_file = proj_dir / "old_project.spectra.json"
    proj_file.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")

    win = MainWindow(project_root=app_root)
    requested: list[str] = []
    win.open_dataset = lambda p, **kwargs: requested.append(str(Path(p).resolve()))

    # 程序化打开返回可检查的待确认状态
    res = win.open_project(proj_file)
    assert res == "pending_confirmation"
    assert win.is_pending_confirmation is True
    # 严格断言：绝对不能静默派发任何文件
    assert requested == []
    # 未确认前禁止保存
    assert win.save_project_action() is False

    # 用户明确确认指向 app_root 中的原文件
    assert win.confirm_legacy_migration({"L_old": orig_file}) is True
    assert win.is_pending_confirmation is False
    # 断言实际解析派发的物理路径是 app_root 下的原目标，而非项目目录下的同名文件
    assert requested == [str(orig_file.resolve())]
    assert len(win.missing_sources) == 0

    # 确认后允许持久化迁移结果
    assert win.save_project_action() is True
    win.close()
    qapp.processEvents()

    # 验证新文件已写入 path_anchor="project_dir"
    migrated_spec = load_project(proj_file)
    assert migrated_spec.path_anchor == "project_dir"


def test_legacy_project_only_one_exists_still_requires_confirmation(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧项目仅单侧存在文件：仍不自动猜测，返回待确认状态。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    only_runtime = app_root / "solo.tif"
    only_runtime.write_bytes(b"runtime solo data")

    proj_dir = tmp_path / "user_proj"
    proj_dir.mkdir()

    raw_json = {
        "schema_version": "1.0",
        "project_id": "legacy_solo",
        "layers": [
            {
                "layer_id": "L_solo",
                "name": "SoloLayer",
                "source": "solo.tif",
                "source_is_relative": True,
                "order": 0,
            }
        ],
    }
    proj_file = proj_dir / "solo.spectra.json"
    proj_file.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")

    win = MainWindow(project_root=app_root)
    requested: list[str] = []
    win.open_dataset = lambda p, **kwargs: requested.append(str(Path(p).resolve()))

    res = win.open_project(proj_file)
    assert res == "pending_confirmation"
    assert win.is_pending_confirmation is True
    assert requested == []

    # 用户确认选择 runtime 侧的目标
    assert win.confirm_legacy_migration({"L_solo": only_runtime}) is True
    assert requested == [str(only_runtime.resolve())]
    win.close()
    qapp.processEvents()


def test_legacy_project_neither_exists_requires_confirmation_and_reports_missing(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧项目两处均不存在文件：确认后如实记录为 missing，不静默挂起。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    proj_dir = tmp_path / "user_proj"
    proj_dir.mkdir()

    raw_json = {
        "schema_version": "1.0",
        "project_id": "legacy_none",
        "layers": [
            {
                "layer_id": "L_none",
                "name": "NoneLayer",
                "source": "ghost.tif",
                "source_is_relative": True,
                "order": 0,
            }
        ],
    }
    proj_file = proj_dir / "ghost.spectra.json"
    proj_file.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")

    win = MainWindow(project_root=app_root)
    requested: list[str] = []
    win.open_dataset = lambda p, **kwargs: requested.append(str(Path(p).resolve()))

    res = win.open_project(proj_file)
    assert res == "pending_confirmation"
    assert win.is_pending_confirmation is True
    assert requested == []

    # 默认确认（未找到 ghost.tif）
    assert win.confirm_legacy_migration() is True
    assert requested == []
    assert len(win.missing_sources) == 1
    assert win.missing_sources[0].tried == (proj_dir / "ghost.tif").resolve()
    win.close()
    qapp.processEvents()


def test_new_format_with_path_anchor_resolves_directly_without_confirmation(
    qapp: QApplication, tmp_path: Path
) -> None:
    """新格式项目具有显式 path_anchor='project_dir'：直接解析项目目录下的相对路径，无需确认。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    (app_root / "data.tif").write_bytes(b"app root data")

    proj_dir = tmp_path / "user_proj"
    proj_dir.mkdir()
    correct_proj_data = proj_dir / "data.tif"
    correct_proj_data.write_bytes(b"project dir data")

    spec = ProjectSpec(
        schema_version="1.0",
        path_anchor="project_dir",
        layers=[
            LayerRecord(
                layer_id="L_new",
                name="NewLayer",
                source="data.tif",
                source_is_relative=True,
                order=0,
            )
        ],
    )
    proj_file = proj_dir / "new_project.spectra.json"
    save_project(proj_file, spec)

    win = MainWindow(project_root=app_root)
    requested: list[str] = []
    win.open_dataset = lambda p, **kwargs: requested.append(str(Path(p).resolve()))

    res = win.open_project(proj_file)
    assert res is True
    assert win.is_pending_confirmation is False
    # 明确验证派发的是 project_dir 下的目标
    assert requested == [str(correct_proj_data.resolve())]
    win.close()
    qapp.processEvents()


def test_legacy_project_cancel_confirmation_leaves_original_file_untouched(
    qapp: QApplication, tmp_path: Path
) -> None:
    """取消确认旧项目：原文件不被重写或修改，工作区清理。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    proj_dir = tmp_path / "user_proj"
    proj_dir.mkdir()

    raw_text = json.dumps(
        {
            "schema_version": "1.0",
            "project_id": "legacy_cancel",
            "layers": [
                {
                    "layer_id": "L_c",
                    "name": "CancelLayer",
                    "source": "c.tif",
                    "source_is_relative": True,
                    "order": 0,
                }
            ],
        },
        indent=2,
    )
    proj_file = proj_dir / "cancel.spectra.json"
    proj_file.write_text(raw_text, encoding="utf-8")

    win = MainWindow(project_root=app_root)
    res = win.open_project(proj_file)
    assert res == "pending_confirmation"

    win.cancel_legacy_confirmation()
    assert win.legacy_status == "cancelled"
    assert win.is_pending_confirmation is False
    assert len(win.layer_panel.layers) == 0

    # 原文件内容毫厘不差
    assert proj_file.read_text(encoding="utf-8") == raw_text
    win.close()
    qapp.processEvents()


def test_legacy_project_with_absolute_paths_resolves_directly_without_blocking(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧 1.0 项目但使用绝对路径：无基准歧义，直接按绝对路径解析派发。"""
    app_root = tmp_path / "runtime_app"
    app_root.mkdir(parents=True)
    proj_dir = tmp_path / "user_proj"
    proj_dir.mkdir()

    abs_target = tmp_path / "explicit_external.tif"
    abs_target.write_bytes(b"explicit external data")

    raw_json = {
        "schema_version": "1.0",
        "project_id": "legacy_abs",
        "layers": [
            {
                "layer_id": "L_abs",
                "name": "AbsLayer",
                "source": str(abs_target.resolve()),
                "source_is_relative": False,
                "order": 0,
            }
        ],
    }
    proj_file = proj_dir / "abs.spectra.json"
    proj_file.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")

    win = MainWindow(project_root=app_root)
    requested: list[str] = []
    win.open_dataset = lambda p, **kwargs: requested.append(str(Path(p).resolve()))

    res = win.open_project(proj_file)
    assert res is True
    assert win.is_pending_confirmation is False
    assert requested == [str(abs_target.resolve())]
    win.close()
    qapp.processEvents()
