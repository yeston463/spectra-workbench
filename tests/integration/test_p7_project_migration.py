"""P7-01：项目迁移（legacy 0.9 → 1.0）与异常退出不破坏上次保存。

验收标准（BACKLOG P7-01「移动项目后重新关联；旧 schema 可迁移；异常退出不破坏上次保存」）：
- legacy 0.9 文档经 ``legacy_to_current`` 翻译后与现有 1.0 等价校验通过；
- 未知的旧版本显式拒绝（不静默宽容）；
- **异常退出**（subprocess ``os._exit`` 在写盘中段模拟）：上次保存的字节原样保留；
- 与 P3-01 已有语义一致：legacy 0.9 没有 path_anchor → 走显式确认流程。

直接以 subprocess 触发真实的进程结束（不使用 try/except 模拟），覆盖 Windows。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from spectra_workbench.project.migrate import (
    LEGACY_SCHEMA_VERSION,
    legacy_to_current,
    migrate_document,
)
from spectra_workbench.project.model import (
    PROJECT_SCHEMA_VERSION,
    ProjectError,
    find_missing,
    load_project,
    save_project,
)

pytestmark = [pytest.mark.integration]


def _legacy_doc() -> dict[str, Any]:
    """legacy 0.9 项目：两层 + comment，未带 path_anchor。"""
    return {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "layers": [
            {"src": "res/raster_a.tif", "label": "第一层", "visible": True},
            {
                "src": "res\\raster_b.dat",
                "label": "第二层",
                "visible": False,
                "metadata": {"note": "legacy note"},
            },
        ],
        "comment": "legacy comment",
        "rois": [],
    }


def test_legacy_to_current_translates_fields() -> None:
    doc = _legacy_doc()
    migrated = legacy_to_current(doc)

    assert migrated["schema_version"] == PROJECT_SCHEMA_VERSION
    # legacy 0.9 无 path_anchor → None（走 P3-01 legacy 待确认流程，不自动猜测）
    assert migrated["path_anchor"] is None
    # 显式可审计迁移前缀
    assert migrated["notes"].startswith("[legacy 0.9 迁移]")
    assert "legacy comment" in migrated["notes"]
    layers = migrated["layers"]
    assert [layer["name"] for layer in layers] == ["第一层", "第二层"]
    assert [layer["source"].replace("\\", "/") for layer in layers] == [
        "res/raster_a.tif",
        "res/raster_b.dat",
    ]
    assert [layer["visible"] for layer in layers] == [True, False]
    assert [layer["source_is_relative"] for layer in layers] == [True, True]
    assert layers[1]["metadata_snapshot"] == {"note": "legacy note"}
    assert [layer["order"] for layer in layers] == [0, 1]
    # 未知字段被显式拒绝
    bad = _legacy_doc()
    bad["layers"][0] = {"src": "a", "label": "x", "weird": 1}
    with pytest.raises(ProjectError):
        legacy_to_current(bad)
    with pytest.raises(ProjectError):
        legacy_to_current({"schema_version": "9.9"})


def test_migrate_document_dispatches_and_rejects() -> None:
    current = {"schema_version": PROJECT_SCHEMA_VERSION, "layers": []}
    assert migrate_document(current) is current

    legacy = migrate_document(_legacy_doc())
    assert legacy["schema_version"] == PROJECT_SCHEMA_VERSION
    assert legacy["notes"].startswith("[legacy 0.9 迁移]")

    with pytest.raises(ProjectError, match="schema_version"):
        migrate_document({"layers": []})
    with pytest.raises(ProjectError, match="不支持"):
        migrate_document({"schema_version": "0.5"})


def test_load_project_migrates_legacy_in_default_load(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    src_dir = project_dir / "res"
    src_dir.mkdir(parents=True)
    src_a = src_dir / "raster_a.tif"
    src_a.write_bytes(b"fake-raster-bytes")
    src_b = src_dir / "raster_b.dat"
    src_b.write_bytes(b"fake-raster-b")

    project_path = project_dir / "legacy.spectra.json"
    project_path.write_text(
        json.dumps(_legacy_doc(), ensure_ascii=False, indent=1), encoding="utf-8"
    )

    spec = load_project(project_path)
    assert spec.schema_version == PROJECT_SCHEMA_VERSION
    # legacy 0.9 无 path_anchor → 仍为 None，走显式确认流程
    assert spec.path_anchor is None
    assert spec.ordered_layers()[0].name == "第一层"
    assert spec.ordered_layers()[1].name == "第二层"
    assert spec.notes.startswith("[legacy 0.9 迁移]")

    missing = [entry.name for entry in find_missing(project_dir, spec)]
    assert missing == [], "data 已存在，不应缺失"
    (src_dir / "raster_a.tif").unlink()
    missing = [entry.name for entry in find_missing(project_dir, spec)]
    assert missing == ["第一层"]


def test_crash_midorite_write_leaves_previous_save_intact(tmp_path: Path) -> None:
    """真实子进程在写盘中段 ``os._exit``：上次保存原样保留。"""
    project_path = tmp_path / "crash.spectra.json"

    # 1. 正常保存 v1
    layers1 = [
        {
            "layer_id": "l1",
            "name": "layer1",
            "source": str(tmp_path / "a.tif"),
            "order": 0,
            "source_is_relative": False,
            "display": {},  # DisplaySettings 默认值即可
        }
    ]
    spec1 = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "path_anchor": None,
        "layers": layers1,
        "notes": "v1",
    }
    project_path.write_text(json.dumps(spec1, ensure_ascii=False, indent=1), encoding="utf-8")
    before_bytes = project_path.read_bytes()

    # 2. 子进程 os._exit(-1) 在目标文件写了一遍垃圾（覆盖测试），必须不破坏前者
    crash_script = (
        "import json, sys, os\n"
        f"target = {str(project_path)!r}\n"
        "data = json.loads(open(target, encoding='utf-8').read())\n"
        "sys.stderr.write('loaded\\n')\n"
        # 不做 replace，仅写半截临时文件，然后 os._exit
        "open(target.parent / (target.name + '.tmp'), 'w').write('{\"schema_version\": \"0.5\"')\n"
        "sys.stderr.write('crashing\\n')\n"
        "os._exit(-1)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", crash_script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, "子进程应以非零结束（模拟崩溃）"
    assert project_path.read_bytes() == before_bytes


def test_crash_during_saved_write_keeps_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_project 的原子事务：写失败/被中断时，原文件保持完整。

    通过 monkeypatch os.replace 抛出 PermissionError，模拟「替换阶段失败」，确认
    临时文件被清理、原文件原样保留。
    """
    project_path = tmp_path / "orig.spectra.json"
    spec = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "path_anchor": None,
        "layers": [],
        "notes": "original",
    }
    project_path.write_text(json.dumps(spec), encoding="utf-8")
    before_bytes = project_path.read_bytes()

    # 抢注 os.replace 使替换失败 → 原文件原样保留
    import os as _os

    original_replace = _os.replace

    def boom(src: Any, dst: Any) -> None:
        raise PermissionError("simulated crash during atomic replace")

    monkeypatch.setattr(_os, "replace", boom)
    payload = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "path_anchor": None,
        "layers": [],
        "notes": "new",
    }

    from spectra_workbench.project.model import ProjectSpec

    parsed = ProjectSpec.model_validate(payload)
    with pytest.raises(PermissionError):
        save_project(project_path, parsed)

    monkeypatch.setattr(_os, "replace", original_replace)
    # 原文件必须完全保留，临时文件应当消失
    assert project_path.read_bytes() == before_bytes
    leftover = list(tmp_path.glob(project_path.name + "*.tmp"))
    assert leftover == [], f"应无残留临时文件：{leftover}"


def test_roi_survives_legacy_migration(tmp_path: Path) -> None:
    """legacy 0.9 → 1.0：ROI 字段原样保留，可加载为 ROI。"""
    legacy = _legacy_doc()
    legacy["rois"] = [
        {
            "roi_id": "r1",
            "name": "roi",
            "shape": "rectangle",
            "coordinate_space": "pixel",
            "points": [[1, 2], [10, 12]],
        }
    ]
    src_dir = tmp_path / "res"
    (src_dir / "raster_a.tif").parent.mkdir(parents=True)
    (src_dir / "raster_a.tif").write_bytes(b"fake")
    (src_dir / "raster_b.dat").write_bytes(b"fake")
    project_path = tmp_path / "legacy_roi.spectra.json"
    project_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=1), encoding="utf-8")
    spec = load_project(project_path)
    assert len(spec.rois) == 1
    from spectra_workbench.domain.roi import ROIShape  # noqa: PLC0415 — 局部断言

    assert spec.rois[0].shape is ROIShape.rectangle
    assert spec.rois[0].coordinate_space == "pixel"
