"""P0-03 验收：Doctor 数据模型、控制台格式化与无 HDF4 容错判定。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
from unittest.mock import patch

import pytest

from spectra_workbench.app import doctor
from spectra_workbench.app.doctor import (
    DoctorReport,
    EngineReport,
    find_project_root,
    format_console_report,
    resolve_gdal_runtime,
)

pytestmark = pytest.mark.unit


def test_doctor_report_overall_ok():
    report = DoctorReport(
        generated_utc="2026-11-03T12:00:00Z",
        project_root="C:/dummy/spectra-workbench",
        overall_ok=True,
        gui_can_start=True,
        envs={
            "ui": EngineReport(
                role="ui",
                status="ok",
                python="3.12.10",
                version_info={"PySide6": "6.9.3", "qt_version": "6.9.3"},
                checks={"isolation_passed": True, "isolation_details": {"osgeo": False}},
                messages=["UI ok"],
            ),
            "gdal": EngineReport(
                role="gdal",
                status="ok",
                python="3.12.14",
                version_info={"gdal_version": "3.13.3", "proj_version": "9.8.1"},
                checks={
                    "gtiff_roundtrip": {"ok": True},
                    "coord_transform": {"ok": True},
                    "resource_dirs": {"gdal_data_valid": True, "proj_data_valid": True},
                    "gui_can_start_without_hdf4": True,
                },
                drivers={"GTiff": True, "ENVI": True, "HDF4": False},
                messages=["GDAL ok"],
            ),
        },
        warnings=["Optional driver HDF4 absent; core unaffected"],
    )

    text = format_console_report(report)
    assert "总体判定: 【通过 ALL OK】" in text
    assert "GUI 启动许可: 允许" in text
    assert "[OK] 1. UI 界面环境" in text
    assert "[OK] 2. GDAL / OGR 引擎" in text
    assert "GTiff 往返: 成功" in text
    assert "坐标转换 (4326->3857): 成功" in text


def test_doctor_report_missing_hdf4_still_allows_gui():
    """验收标准：无 HDF4 仍可打开 GUI。"""
    report = DoctorReport(
        generated_utc="2026-11-03T12:00:00Z",
        project_root="C:/dummy",
        overall_ok=False,
        gui_can_start=True,
        envs={
            "ui": EngineReport(role="ui", status="ok"),
            "gdal": EngineReport(
                role="gdal",
                status="warning",
                checks={"gui_can_start_without_hdf4": True},
                drivers={"HDF4": False},
            ),
        },
        warnings=["HDF4 driver absent"],
    )
    text = format_console_report(report)
    assert "GUI 启动许可: 允许" in text
    assert "【提示与警告】" in text


def test_doctor_report_isolation_violation_blocks_gui():
    """工程约束 1：GUI 进程若泄露原生地理依赖，判定失败。"""
    report = DoctorReport(
        generated_utc="2026-11-03T12:00:00Z",
        project_root="C:/dummy",
        overall_ok=False,
        gui_can_start=False,
        envs={
            "ui": EngineReport(
                role="ui",
                status="error",
                checks={"isolation_passed": False},
                messages=["Isolation violation: osgeo is importable in UI process!"],
            )
        },
        errors=["Isolation violation in UI"],
    )
    text = format_console_report(report)
    assert "GUI 启动许可: 拒绝" in text
    assert "【阻断性错误】" in text


def test_doctor_cli_failure_marks_engine_and_overall_error():
    """缺陷 3 验收：注入 CLI 退出码 42，Doctor 必须报告 CLI 不可用、引擎错误且总体失败。"""
    root = find_project_root()
    env_report_file = root / "docs" / "progress" / "runs" / "env_report.json"
    env_report = json.loads(env_report_file.read_text(encoding="utf-8-sig"))
    probes = {role: entry["probe"] for role, entry in env_report["envs"].items()}

    def fake_probe(_python, _script, role, extra_env=None):
        return 0, copy.deepcopy(probes[role]), ""

    with patch.object(doctor, "probe_role", side_effect=fake_probe), \
         patch.object(doctor, "_run_subproc", return_value=(42, "", "injected CLI failure")):
        report = doctor.run_doctor(root)

        assert report.overall_ok is False
        assert report.envs["gdal"].status == "error"
        assert report.envs["gdal"].cli_tools["gdal"]["available"] is False
        assert report.envs["gdal"].cli_tools["gdal"]["exit_code"] == 42
        assert "42" in report.envs["gdal"].cli_tools["gdal"]["error"]

        assert report.envs["rasterio"].status == "error"
        assert report.envs["rasterio"].cli_tools["rio"]["available"] is False
        assert report.envs["rasterio"].cli_tools["rio"]["exit_code"] == 42

        assert len(report.errors) > 0

        # main() 必须返回 1
        with patch.object(doctor, "run_doctor", return_value=report), \
             patch.object(doctor, "save_doctor_report"), \
             patch.object(sys, "argv", ["doctor", "--json"]), \
             contextlib.redirect_stdout(io.StringIO()):
            exit_code = doctor.main()
        assert exit_code == 1


def test_doctor_gtiff_failure_causes_main_exit_one_and_ui_only_exit_zero():
    """缺陷 4 验收：GTiff 核心失败时全量模式退出码 1，--ui-only 模式退出码 0。"""
    root = find_project_root()
    env_report_file = root / "docs" / "progress" / "runs" / "env_report.json"
    env_report = json.loads(env_report_file.read_text(encoding="utf-8-sig"))
    probes = {role: entry["probe"] for role, entry in env_report["envs"].items()}

    failed_probes = copy.deepcopy(probes)
    failed_probes["gdal"]["gdal"]["gtiff_roundtrip"] = {"ok": False, "error": "injected write/read failure"}

    def failing_gdal_probe(_python, _script, role, extra_env=None):
        return 0, copy.deepcopy(failed_probes[role]), ""

    with patch.object(doctor, "probe_role", side_effect=failing_gdal_probe), \
         patch.object(doctor, "_run_subproc", return_value=(0, "GDAL 3.13.3", "")):
        report = doctor.run_doctor(root)

    assert report.overall_ok is False
    assert report.gui_can_start is False  # 核心 GTiff 往返失败不可启动
    assert report.envs["gdal"].status == "error"
    assert len(report.errors) > 0

    # 全量模式必须退出 1
    with patch.object(doctor, "run_doctor", return_value=report), \
         patch.object(doctor, "save_doctor_report"), \
         patch.object(sys, "argv", ["doctor", "--json"]), \
         contextlib.redirect_stdout(io.StringIO()):
        assert doctor.main() == 1

    # --ui-only 模式因 UI 依赖完备返回 0
    with patch.object(doctor, "run_doctor", return_value=report), \
         patch.object(doctor, "save_doctor_report"), \
         patch.object(sys, "argv", ["doctor", "--json", "--ui-only"]), \
         contextlib.redirect_stdout(io.StringIO()):
        assert doctor.main() == 0


def test_doctor_empty_cli_version_marks_error():
    """CLI 返回退出码 0 但输出为空时，不得判为 available=True。"""
    root = find_project_root()
    env_report_file = root / "docs" / "progress" / "runs" / "env_report.json"
    env_report = json.loads(env_report_file.read_text(encoding="utf-8-sig"))
    probes = {role: entry["probe"] for role, entry in env_report["envs"].items()}

    def fake_probe(_python, _script, role, extra_env=None):
        return 0, copy.deepcopy(probes[role]), ""

    with patch.object(doctor, "probe_role", side_effect=fake_probe), \
         patch.object(doctor, "_run_subproc", return_value=(0, "", "")):
        report = doctor.run_doctor(root)

        assert report.overall_ok is False
        assert report.envs["gdal"].cli_tools["gdal"]["available"] is False
        assert report.envs["gdal"].cli_tools["gdal"]["error"] == "empty version output from CLI"
        assert report.envs["rasterio"].cli_tools["rio"]["available"] is False


def test_doctor_resolve_gdal_runtime(monkeypatch, tmp_path):
    """测试统一运行时解析：环境变量优先，其次 env_report.json，最后系统默认。"""
    root = find_project_root()

    # 1. 默认解析
    res = resolve_gdal_runtime(root)
    assert res["python"] is not None
    assert "PATH" in res["env"]

    # 2. 环境变量优先覆盖
    custom_py = tmp_path / "mock_python.exe"
    custom_py.write_text("echo", encoding="utf-8")
    monkeypatch.setenv("SPECTRA_GDAL_PYTHON", str(custom_py))
    res2 = resolve_gdal_runtime(root)
    assert res2["python"] == custom_py
