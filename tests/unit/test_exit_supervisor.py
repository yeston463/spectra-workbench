"""S1 exit gate: runner/supervisor contract tests.

These tests deliberately do not run the full suite. They prove the pieces that
decide whether a run is release-eligible:

- a bypass run never claims clean_exit_verified;
- a clean supervisor result needs a real pytest result and a zero OS exit code;
- pytest_runner writes the pytest exit code before any bypass exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_with_timeout  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_bypass_never_claims_clean_exit(tmp_path: Path) -> None:
    result_path = tmp_path / "bypass.result.json"
    rc = run_with_timeout.run(
        [sys.executable, "-c", "pass"],
        30.0,
        tmp_path / "bypass.log",
        result_json=result_path,
        exit_mode="bypass",
        run_id="test-bypass",
    )
    assert rc == 0
    result = _read_json(result_path)
    assert result["exit_mode"] == "bypass"
    assert result["bypass_finalization"] is True
    assert result["child_exit_code"] == 0
    assert result["clean_exit_verified"] is False


def test_clean_without_pytest_result_is_not_green(tmp_path: Path) -> None:
    result_path = tmp_path / "clean.result.json"
    rc = run_with_timeout.run(
        [sys.executable, "-c", "pass"],
        30.0,
        tmp_path / "clean.log",
        result_json=result_path,
        exit_mode="clean",
        run_id="test-clean-no-pytest",
    )
    assert rc == 0
    result = _read_json(result_path)
    assert result["pytest_exit_code"] is None
    assert result["child_exit_code"] == 0
    assert result["clean_exit_verified"] is False, (
        "没有 pytest 结果时，外部监督器不得把普通命令当成 Clean 验收"
    )


def test_pytest_runner_bypass_writes_real_pytest_code(tmp_path: Path) -> None:
    test_file = tmp_path / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result_path = tmp_path / "runner.result.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "pytest_runner.py"),
            "--mode",
            "bypass",
            "--result-json",
            str(result_path),
            "--",
            "-q",
            str(test_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = _read_json(result_path)
    assert result["pytest_exit_code"] == 0
    assert result["bypass_finalization"] is True
    assert result["clean_exit_verified"] is False
    assert "[exit-mode] BYPASS" in proc.stderr


def test_pytest_runner_clean_records_code_without_claiming_green(tmp_path: Path) -> None:
    test_file = tmp_path / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result_path = tmp_path / "runner_clean.result.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "pytest_runner.py"),
            "--mode",
            "clean",
            "--result-json",
            str(result_path),
            "--",
            "-q",
            str(test_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = _read_json(result_path)
    assert result["pytest_exit_code"] == 0
    assert result["bypass_finalization"] is False
    # runner 只能记录 pytest 结果；是否 clean 由外层监督器结合真实 OS 退出码判定。
    assert result["clean_exit_verified"] is False


def test_source_snapshot_includes_git_identity() -> None:
    import source_snapshot

    snapshot = source_snapshot.compute_source_snapshot(ROOT)
    assert snapshot.startswith("git:")
    assert "diff=" in snapshot and "untracked=" in snapshot
