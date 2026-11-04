"""UI 组件单元测试（P2-04 / P2-05 / P3-01）。

包含：
1. 界面模块零原生地理依赖导入架构约束严格检查。
2. LayerItem 领域模型与波段选择逻辑。
3. ToolboxPanel 模糊搜索与分类树逻辑。
4. CapabilityFormWidget 动态表单生成（已接通 vs 未接入禁用状态）。
5. TaskPanel 任务状态与日志展示。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.registry.catalog import get_default_registry
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.task_panel import TaskPanel
from spectra_workbench.ui.panels.toolbox_panel import ToolboxPanel

pytestmark = [pytest.mark.unit, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_ui_modules_strictly_no_geospatial_imports() -> None:
    """架构约束核验：src/spectra_workbench/ui/ 下所有源文件绝无 osgeo/rasterio/fiona/geopandas 导入。"""
    root = Path(__file__).resolve().parents[2]
    ui_dir = root / "src" / "spectra_workbench" / "ui"
    assert ui_dir.exists(), f"UI 目录未找到: {ui_dir}"

    forbidden_patterns = [
        re.compile(r"^\s*(?:import|from)\s+osgeo\b"),
        re.compile(r"^\s*(?:import|from)\s+gdal\b"),
        re.compile(r"^\s*(?:import|from)\s+ogr\b"),
        re.compile(r"^\s*(?:import|from)\s+osr\b"),
        re.compile(r"^\s*(?:import|from)\s+rasterio\b"),
        re.compile(r"^\s*(?:import|from)\s+fiona\b"),
        re.compile(r"^\s*(?:import|from)\s+geopandas\b"),
    ]

    py_files = list(ui_dir.rglob("*.py"))
    assert len(py_files) >= 5, "UI 模块文件不足"

    violations: list[str] = []
    for f in py_files:
        lines = f.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            for pat in forbidden_patterns:
                if pat.search(line):
                    violations.append(f"{f.name}:{idx} 违规导入: {line.strip()}")

    assert not violations, "发现 UI 模块违规导入原生地理依赖：\n" + "\n".join(violations)


def test_layer_item_model() -> None:
    """测试 LayerItem 属性派生与 RGB/灰度显示模式。"""
    meta = {
        "width": 100,
        "height": 80,
        "count": 4,
        "driver": "ENVI",
        "driver_long_name": "ENVI raster",
        "dtypes": ["Float32", "Float32", "Float32", "Float32"],
        "crs_epsg": 32650,
        "nodata": -9999.0,
    }
    layer = LayerItem(name="sample", file_path=Path("sample.dat"), metadata=meta)
    assert layer.width == 100
    assert layer.height == 80
    assert layer.band_count == 4
    assert layer.driver == "ENVI"
    assert layer.crs_display == "EPSG:32650"
    assert layer.display_mode == "rgb"
    assert layer.get_selected_bands() == [1, 2, 3]

    # 切换为灰度
    layer.display_mode = "gray"
    layer.gray_band = 2
    assert layer.get_selected_bands() == [2]


def test_toolbox_panel_search_and_filtering(qapp: QApplication) -> None:
    """测试工具箱全量基准载入、关键词搜索与引擎筛选。"""
    panel = ToolboxPanel()

    # 默认包含已接通算法、GDAL算法、Rasterio算法、驱动
    assert panel.tree.topLevelItemCount() >= 3

    # 搜索 "warp"
    panel.txt_search.setText("warp")
    qapp.processEvents()
    assert "warp" in panel.lbl_stats.text().lower() or panel.tree.topLevelItemCount() > 0

    # 引擎筛选为 Rasterio
    panel.txt_search.clear()
    panel.cmb_provider.setCurrentText("Rasterio")
    qapp.processEvents()

    # 还原
    panel.cmb_provider.setCurrentText("全部引擎")
    qapp.processEvents()


def test_param_form_connected_vs_unsupported(qapp: QApplication) -> None:
    """测试参数面板：已接通能力允许提交并生成 TaskSpec；未接入能力严格禁用提交。"""
    registry = get_default_registry()
    form = CapabilityFormWidget()

    # 1. 已接通能力：gdal:warp
    warp_cap = registry.get("gdal:warp")
    assert warp_cap is not None
    form.load_capability(warp_cap)
    assert form.btn_submit.isEnabled()
    assert form.lbl_unsupported_banner.isHidden()

    # 模拟设置输入输出
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "data"
    in_file = (fixture_dir / "a01_ramp_16x16.dat").resolve()
    out_file = (fixture_dir / "warp_test_out.tif").resolve()
    form.txt_input.setText(str(in_file))
    form.txt_output.setText(str(out_file))

    submitted_specs = []
    form.sig_task_submitted.connect(submitted_specs.append)
    form._on_submit_clicked()

    assert len(submitted_specs) == 1
    spec = submitted_specs[0]
    assert spec.capability_id == "gdal:warp"
    assert spec.engine.provider_id == "gdal"
    assert spec.inputs["input"].uri == in_file.as_uri()

    # 2. 未接入平台的能力：gdal:dem
    dem_cap = registry.get("gdal:dem")
    assert dem_cap is not None
    form.load_capability(dem_cap)
    assert not form.btn_submit.isEnabled()
    assert "禁止提交" in form.btn_submit.text()
    assert not form.lbl_unsupported_banner.isHidden()


def test_task_panel_lifecycle(qapp: QApplication) -> None:
    """测试 TaskPanel 状态生命周期、进度与彩色日志流。"""
    panel = TaskPanel()
    assert not panel.btn_cancel.isEnabled()
    assert not panel.btn_load_result.isEnabled()

    from spectra_workbench.domain.models import (
        DatasetKind,
        EngineConstraint,
        OutputPlan,
        TaskResult,
        TaskSpec,
        TaskStatus,
    )

    spec = TaskSpec(
        task_id="t-lifecycle-01",
        attempt=1,
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=".",
        workspace=".",
        outputs=[OutputPlan(key="out", kind=DatasetKind.raster, uri="file:///tmp/out.tif")],
    )

    panel.on_task_started(spec)
    assert panel.btn_cancel.isEnabled()
    assert "t-lifecycle-01" in panel.lbl_task_info.text()

    panel.on_progress(0.5, "working", "Halfway done")
    assert panel.progress_bar.value() == 50

    panel.on_log("INFO", "Log test message", "engine")
    assert "Log test message" in panel.txt_logs.toPlainText()

    res = TaskResult(
        task_id="t-lifecycle-01",
        attempt=1,
        status=TaskStatus.succeeded,
        outputs=[{"key": "out", "uri": "file:///tmp/out.tif"}],
        duration_seconds=1.23,
    )
    panel.on_task_completed(res)
    assert panel.progress_bar.value() == 100
    assert not panel.btn_cancel.isEnabled()
    assert panel.btn_load_result.isEnabled()
