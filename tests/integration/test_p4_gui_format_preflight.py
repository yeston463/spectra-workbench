"""S4 GUI format preflight: unsupported formats are blocked before TaskSpec."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.registry.catalog import get_default_registry
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.gui]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture(autouse=True)
def _require_qapp(qapp: QApplication) -> QApplication:
    return qapp


def _form(tmp_path: Path, cid: str = "gdal:translate") -> CapabilityFormWidget:
    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    cap = get_default_registry().get(cid)
    assert cap is not None
    form.load_capability(cap)
    return form


def _metadata(dtype: str = "Float32", *, bands: int = 1, alpha: bool = False) -> dict:
    return {
        "count": bands,
        "dtypes": [dtype] * bands,
        "bands": [
            {
                "band": i + 1,
                "dtype": dtype,
                "nodata": None,
                "mask_flags": 6 if alpha else 1,
                "has_alpha": alpha and i == bands - 1,
            }
            for i in range(bands)
        ],
    }


def test_gui_rejects_png_float32_by_metadata(tmp_path: Path) -> None:
    form = _form(tmp_path)
    form.set_metadata_provider(lambda *_: _metadata("Float32"))
    try:
        error = form._preflight_output_format(
            "gdal:translate", [tmp_path / "a.tif"], "PNG", {"format": "PNG"}
        )
        assert error and "PNG" in error and "Float32" in error
    finally:
        form.close()


def test_gui_rejects_jpeg_alpha_by_metadata(tmp_path: Path) -> None:
    form = _form(tmp_path)
    form.set_metadata_provider(lambda *_: _metadata("Byte", bands=4, alpha=True))
    try:
        error = form._preflight_output_format(
            "gdal:translate", [tmp_path / "a.tif"], "JPEG", {"format": "JPEG"}
        )
        assert error and "JPEG" in error
    finally:
        form.close()


def test_gui_accepts_png_byte_rgb(tmp_path: Path) -> None:
    form = _form(tmp_path)
    form.set_metadata_provider(lambda *_: _metadata("Byte", bands=3))
    try:
        error = form._preflight_output_format(
            "gdal:translate", [tmp_path / "a.tif"], "PNG", {"format": "PNG"}
        )
        assert error is None
    finally:
        form.close()


def test_gui_rejects_png_nodata_without_metadata(tmp_path: Path) -> None:
    form = _form(tmp_path)
    try:
        error = form._preflight_output_format(
            "gdal:merge",
            [tmp_path / "a.tif", tmp_path / "b.tif"],
            "PNG",
            {"format": "PNG", "dstNodata": 0},
        )
        assert error and "NoData" in error
    finally:
        form.close()


def test_gui_submit_is_blocked_before_spec(tmp_path: Path) -> None:
    src = tmp_path / "a.tif"
    src.write_bytes(b"placeholder")
    out = tmp_path / "out.png"
    form = _form(tmp_path)
    form.set_metadata_provider(lambda *_: _metadata("Float32"))
    emitted: list = []
    form.sig_task_submitted.connect(emitted.append)
    try:
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(out))
        form.cmb_format.setCurrentText("PNG")
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning:
            form._on_submit_clicked()
        assert warning.called
        assert emitted == [], "格式不支持时不得发出 TaskSpec"
    finally:
        form.close()


def test_main_window_wires_metadata_provider(qapp, tmp_path: Path) -> None:
    win = MainWindow(project_root=tmp_path)
    try:
        assert win.form_widget._metadata_provider is not None
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
