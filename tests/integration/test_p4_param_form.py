"""P4-01 参数表单测试：裁剪、重采样与输出网格参数从 GUI 正确下发。

直接测试参数采集方法 `_collect_grid_params`（而不是打桩 TaskSpec）：
既稳定，又能覆盖「非法输入必须被拒绝」的分支。

验证要点：
- 留空的控件**不得**产生任何参数（不得传 0 或空串给引擎）；
- 填写的裁剪窗口、输出像元尺寸、输出尺寸必须正确进入参数；
- 非法输入（元素个数不对、非整数、宽高非正）必须被拒绝并给出提示；
- `targetAlignedPixels` 只对 Warp 生效，Translate 不得携带。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget

pytestmark = [pytest.mark.integration, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


@pytest.fixture
def form(qapp: QApplication, tmp_path: Path):
    w = CapabilityFormWidget()
    w.set_project_root(tmp_path)
    yield w
    w.close()
    w.deleteLater()
    qapp.processEvents()


def test_empty_controls_produce_no_extra_params(form) -> None:
    """留空的控件不得产生任何参数。"""
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    for key in ("srcWin", "xRes", "yRes", "width", "height", "targetAlignedPixels"):
        assert key not in params, f"留空时不应产生 {key}，实际 {params}"


def test_clip_window_is_forwarded(form) -> None:
    """填写裁剪窗口后，srcWin 必须以整数列表进入参数。"""
    form.txt_clip.setText("10,20,100,80")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    assert params.get("srcWin") == [10, 20, 100, 80], f"srcWin 不符：{params}"


def test_resolution_and_size_are_forwarded(form) -> None:
    """输出像元尺寸与输出尺寸必须进入参数。"""
    form.spin_xres.setValue(2.5)
    form.spin_yres.setValue(0.25)
    form.txt_outsize.setText("512,256")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    assert params.get("xRes") == pytest.approx(2.5)
    assert params.get("yRes") == pytest.approx(0.25)
    assert params.get("width") == 512
    assert params.get("height") == 256


def test_zero_resolution_means_unspecified(form) -> None:
    """分辨率控件的 0 是「不指定」哨兵，绝不能作为参数下发。"""
    form.spin_xres.setValue(0.0)
    form.spin_yres.setValue(0.0)
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    assert "xRes" not in params, "0 表示不指定，不得下发"
    assert "yRes" not in params, "0 表示不指定，不得下发"


def test_invalid_clip_window_is_rejected(form) -> None:
    """非法裁剪窗口（个数不对）必须被拒绝。"""
    form.txt_clip.setText("10,20,100")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is not None, "非法裁剪窗口必须报错"
    assert "4" in err
    assert "srcWin" not in params


def test_non_integer_clip_window_is_rejected(form) -> None:
    """非整数裁剪窗口必须被拒绝。"""
    form.txt_clip.setText("a,b,c,d")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is not None, "非整数裁剪窗口必须报错"
    assert "srcWin" not in params


def test_non_positive_clip_size_is_rejected(form) -> None:
    """裁剪窗口宽高非正必须被拒绝。"""
    form.txt_clip.setText("10,20,0,80")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is not None, "非正裁剪尺寸必须报错"
    assert "srcWin" not in params


def test_invalid_output_size_is_rejected(form) -> None:
    """非法输出尺寸必须被拒绝。"""
    form.txt_outsize.setText("512")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is not None, "非法输出尺寸必须报错"
    assert "width" not in params


def test_non_positive_output_size_is_rejected(form) -> None:
    """非正输出尺寸必须被拒绝。"""
    form.txt_outsize.setText("0,256")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is not None
    assert "width" not in params


def test_target_aligned_only_for_warp(form) -> None:
    """targetAlignedPixels 只对 Warp 生效。"""
    form.chk_tap.setChecked(True)
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    assert "targetAlignedPixels" not in params, "Translate 不得携带 targetAlignedPixels"

    params2, err2 = form._collect_grid_params(is_warp=True)
    assert err2 is None
    assert params2.get("targetAlignedPixels") is True, "Warp 应下发 targetAlignedPixels"


def test_p401_controls_exist_and_translate_loads_capability(form) -> None:
    """P4-01 控件必须存在，且加载真实能力描述符后仍可用。"""
    from spectra_workbench.registry.catalog import get_default_registry

    cap = get_default_registry().get("gdal:translate")
    assert cap is not None, "能力注册表中找不到 gdal:translate"

    form.load_capability(cap)
    for attr in ("txt_clip", "spin_xres", "spin_yres", "txt_outsize", "chk_tap"):
        assert hasattr(form, attr), f"缺少 P4-01 控件 {attr}"

    # 加载后仍能正确采集参数
    form.txt_clip.setText("1,2,3,4")
    params, err = form._collect_grid_params(is_warp=False)
    assert err is None
    assert params.get("srcWin") == [1, 2, 3, 4]
