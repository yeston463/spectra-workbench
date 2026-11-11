"""S1 supervisor negative tests.

The supervisor is a release gate, so the tests focus on refusing green:
stale results, identity/mode mismatch, missing/corrupt/incomplete child
evidence, residual descendants, unavailable process observation, cleanup
failures, timeouts and snapshot enumeration gaps.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_with_timeout  # noqa: E402
import source_snapshot  # noqa: E402


def _snapshot() -> str:
    return source_snapshot.compute_source_snapshot_details(ROOT).value


def _fake_child_script(
    *,
    actual_mode: str = "clean",
    rc: int = 0,
    overrides: dict | None = None,
    cleanup_errors: list[str] | None = None,
    write_cleanup: bool = True,
) -> str:
    overrides = overrides or {}
    cleanup_errors = cleanup_errors or []
    return f"""
import json, os, pathlib
child = pathlib.Path(os.environ["SW_CHILD_RESULT_JSON"])
expected = os.environ["SW_EXPECTED_EXIT_MODE"]
actual = {actual_mode!r}
rc = {rc!r}
data = {{
    "schema_version": "1.0",
    "run_id": os.environ["SW_RUN_ID"],
    "source_snapshot": os.environ["SW_SOURCE_SNAPSHOT"],
    "source_snapshot_verified": True,
    "exit_mode": actual,
    "expected_exit_mode": expected,
    "bypass_finalization": actual == "bypass",
    "pytest_exit_code": rc,
    "child_exit_code": None,
    "clean_exit_verified": False,
    "cleanup_check_complete": True,
    "cleanup_errors": {cleanup_errors!r},
    "residual_processes": [],
    "pytest_was_run": True,
    "completed": True,
    "started_at": "2026-11-06T00:00:00+00:00",
    "finished_at": "2026-11-06T00:00:01+00:00",
    "runner_error": None,
}}
data.update({overrides!r})
child.write_text(json.dumps(data), encoding="utf-8")
if {write_cleanup!r}:
    cleanup_path = os.environ.get("SW_CLEANUP_REPORT")
    if cleanup_path:
        pathlib.Path(cleanup_path).write_text(json.dumps({{
            "schema_version": "1.0",
            "complete": True,
            "errors": {cleanup_errors!r},
            "checks": ["fake"],
            "pytest_exitstatus": rc,
        }}), encoding="utf-8")
raise SystemExit(rc)
"""


def _run(
    tmp_path: Path,
    cmd: list[str],
    *,
    exit_mode: str = "clean",
    timeout: float = 60.0,
    run_id: str = "test-run",
) -> tuple[int, dict]:
    final = tmp_path / "final.result.json"
    child = tmp_path / "child.result.json"
    cleanup = tmp_path / "cleanup.json"
    rc = run_with_timeout.run(
        cmd,
        timeout,
        tmp_path / "run.log",
        result_json=final,
        child_result_json=child,
        cleanup_report_json=cleanup,
        exit_mode=exit_mode,
        run_id=run_id,
    )
    return rc, json.loads(final.read_text(encoding="utf-8"))


def test_old_result_file_is_not_used_as_current_evidence(tmp_path: Path) -> None:
    final = tmp_path / "final.result.json"
    old = {
        "schema_version": "1.0",
        "run_id": "old-run",
        "source_snapshot": "old",
        "exit_mode": "clean",
        "clean_exit_verified": True,
        "pytest_exit_code": 0,
        "child_exit_code": 0,
    }
    final.write_text(json.dumps(old), encoding="utf-8")
    rc = run_with_timeout.run(
        [sys.executable, "-c", "pass"],
        30,
        tmp_path / "old.log",
        result_json=final,
        child_result_json=tmp_path / "child.result.json",
        cleanup_report_json=tmp_path / "cleanup.json",
        exit_mode="clean",
        run_id="new-run",
    )
    result = json.loads(final.read_text(encoding="utf-8"))
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["run_id"] == "new-run"
    assert result["pytest_exit_code"] is None
    assert result["clean_exit_verified"] is False
    assert "child_result_missing" in " ".join(result["validation"]["reasons"])


def test_clean_accepts_valid_child_evidence(tmp_path: Path) -> None:
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", _fake_child_script()],
        exit_mode="clean",
    )
    assert rc == 0
    assert result["clean_exit_verified"] is True
    assert result["process_observation"] == "job"
    assert result["residual_processes"] == []


def test_missing_child_result_is_not_green(tmp_path: Path) -> None:
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", "pass"],
        exit_mode="clean",
    )
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert result["pytest_exit_code"] is None


def test_corrupt_child_result_is_not_green(tmp_path: Path) -> None:
    cmd = [
        sys.executable,
        "-c",
        "import os,pathlib; pathlib.Path(os.environ['SW_CHILD_RESULT_JSON']).write_text('{broken')",
    ]
    rc, result = _run(tmp_path, cmd, exit_mode="clean")
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert "invalid_json" in " ".join(result["validation"]["reasons"])


def test_wrong_run_id_is_not_green(tmp_path: Path) -> None:
    cmd = [
        sys.executable,
        "-c",
        _fake_child_script(overrides={"run_id": "other-run"}),
    ]
    rc, result = _run(tmp_path, cmd, exit_mode="clean")
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert "child_run_id_mismatch" in result["validation"]["reasons"]


def test_wrong_snapshot_is_not_green(tmp_path: Path) -> None:
    cmd = [
        sys.executable,
        "-c",
        _fake_child_script(overrides={"source_snapshot": "git:other"}),
    ]
    rc, result = _run(tmp_path, cmd, exit_mode="clean")
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert "child_source_snapshot_mismatch" in result["validation"]["reasons"]


def test_incomplete_child_result_is_not_green(tmp_path: Path) -> None:
    cmd = [
        sys.executable,
        "-c",
        _fake_child_script(overrides={"completed": False, "finished_at": None}),
    ]
    rc, result = _run(tmp_path, cmd, exit_mode="clean")
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert "child_not_completed" in result["validation"]["reasons"]


def test_actual_bypass_cannot_satisfy_clean(tmp_path: Path) -> None:
    child = tmp_path / "child.result.json"
    cleanup = tmp_path / "cleanup.json"
    cmd = [
        sys.executable,
        str(SCRIPTS / "pytest_runner.py"),
        "--mode",
        "bypass",
        "--result-json",
        str(child),
        "--",
        "-q",
    ]
    rc = run_with_timeout.run(
        cmd,
        60,
        tmp_path / "mode.log",
        result_json=tmp_path / "final.result.json",
        child_result_json=child,
        cleanup_report_json=cleanup,
        exit_mode="clean",
        run_id="mode-run",
    )
    result = json.loads((tmp_path / "final.result.json").read_text(encoding="utf-8"))
    assert rc != 0
    assert result["clean_exit_verified"] is False
    assert result["actual_exit_mode"] == "bypass"
    assert any("mode_mismatch" in r for r in result["validation"]["reasons"])


def test_job_assign_precedes_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    original_assign = run_with_timeout.ProcessJob.assign
    original_resume = run_with_timeout.ProcessJob.resume

    def assign(self, pid, handle):
        events.append("assign")
        return original_assign(self, pid, handle)

    def resume(self, pid, handle):
        events.append("resume")
        return original_resume(self, pid, handle)

    monkeypatch.setattr(run_with_timeout.ProcessJob, "assign", assign)
    monkeypatch.setattr(run_with_timeout.ProcessJob, "resume", resume)
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", _fake_child_script()],
        exit_mode="clean",
    )
    assert rc == 0
    assert events[:2] == ["assign", "resume"]
    assert result["clean_exit_verified"] is True


def test_resume_failure_kills_suspended_child_and_is_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_with_timeout.ProcessJob, "resume", lambda self, pid, handle: False)
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", "pass"],
        exit_mode="clean",
    )
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert any("job_start_failure" in e for e in result["cleanup_errors"])
    assert result["process_observation"] == "unknown"


def test_child_is_suspended_until_job_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CREATE_SUSPENDED：assign 回调执行时子进程尚未运行任何用户代码。"""
    marker = tmp_path / "child_started.txt"
    observed: list[bool] = []
    original_assign = run_with_timeout.ProcessJob.assign

    def assign(self, pid, handle):
        observed.append(marker.exists())
        return original_assign(self, pid, handle)

    monkeypatch.setattr(run_with_timeout.ProcessJob, "assign", assign)
    code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('started')"
    rc, result = _run(tmp_path, [sys.executable, "-c", code], exit_mode="clean")
    assert observed == [False], "assign 时子进程不应已经执行用户代码"
    assert marker.exists(), "resume 后子进程应正常执行"
    assert rc != 0 and result["clean_exit_verified"] is False


def test_residual_descendant_is_detected_and_cleaned(tmp_path: Path) -> None:
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "raise SystemExit(0)"
    )
    rc, result = _run(tmp_path, [sys.executable, "-c", script], exit_mode="clean")
    assert rc != 0
    assert result["clean_exit_verified"] is False
    assert result["process_observation"] == "job"
    assert result["residual_processes"], "父进程退出后仍存活的子进程必须被记录"


def test_process_observation_failure_is_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_with_timeout.ProcessJob, "assign", lambda self, pid, handle: False)
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", _fake_child_script()],
        exit_mode="clean",
    )
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert result["process_observation"] == "unknown"


def test_cleanup_error_blocks_green(tmp_path: Path) -> None:
    cmd = [
        sys.executable,
        "-c",
        _fake_child_script(cleanup_errors=["injected cleanup failure"]),
    ]
    rc, result = _run(tmp_path, cmd, exit_mode="clean")
    assert rc == run_with_timeout.EXIT_RESULT_INVALID
    assert result["clean_exit_verified"] is False
    assert any("cleanup" in reason for reason in result["validation"]["reasons"])


def test_real_cleanup_report_error_blocks_green(tmp_path: Path) -> None:
    """真实 pytest conftest 写入 cleanup error 时，runner/supervisor 必须拒绝绿色。"""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "conftest.py").write_text(
        """
import json, os

def pytest_sessionfinish(session, exitstatus):
    p = os.environ.get("SW_CLEANUP_REPORT")
    if p:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "1.0",
                "complete": True,
                "errors": ["injected cleanup failure"],
                "checks": ["temp_conftest"],
                "pytest_exitstatus": exitstatus,
            }, f)
""",
        encoding="utf-8",
    )
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    child = tmp_path / "child.result.json"
    cmd = [
        sys.executable,
        str(SCRIPTS / "pytest_runner.py"),
        "--mode",
        "clean",
        "--result-json",
        str(child),
        "--",
        "-q",
        str(project / "test_ok.py"),
    ]
    rc = run_with_timeout.run(
        cmd,
        120,
        tmp_path / "cleanup_error.log",
        result_json=tmp_path / "final.result.json",
        child_result_json=child,
        cleanup_report_json=tmp_path / "cleanup.json",
        exit_mode="clean",
        run_id="cleanup-error-run",
    )
    result = json.loads((tmp_path / "final.result.json").read_text(encoding="utf-8"))
    assert rc != 0
    assert result["clean_exit_verified"] is False
    assert any("injected cleanup failure" in str(e) for e in result["cleanup_errors"])


def test_timeout_is_not_green(tmp_path: Path) -> None:
    rc, result = _run(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(10)"],
        exit_mode="clean",
        timeout=1.0,
    )
    assert rc == 124
    assert result["clean_exit_verified"] is False
    assert result["timed_out"] is True


def test_real_failed_pytest_is_not_green(tmp_path: Path) -> None:
    test_file = tmp_path / "test_bad.py"
    test_file.write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    child = tmp_path / "child.result.json"
    cmd = [
        sys.executable,
        str(SCRIPTS / "pytest_runner.py"),
        "--mode",
        "clean",
        "--result-json",
        str(child),
        "--",
        "-q",
        str(test_file),
    ]
    rc = run_with_timeout.run(
        cmd,
        120,
        tmp_path / "failed.log",
        result_json=tmp_path / "final.result.json",
        child_result_json=child,
        cleanup_report_json=tmp_path / "cleanup.json",
        exit_mode="clean",
        run_id="failed-run",
    )
    result = json.loads((tmp_path / "final.result.json").read_text(encoding="utf-8"))
    assert rc != 0
    assert result["clean_exit_verified"] is False


def test_untracked_nested_content_changes_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    nested = repo / "newtests" / "nested"
    nested.mkdir(parents=True)
    f1 = nested / "test one.py"
    f2 = nested / "中文 文件.py"
    f1.write_text("value = 1\n", encoding="utf-8")
    f2.write_text("value = 1\n", encoding="utf-8")
    first = source_snapshot.compute_source_snapshot_details(repo)
    assert first.verified is True
    assert first.untracked_count == 2
    assert {item["path"] for item in first.untracked_files} == {
        "newtests/nested/test one.py",
        "newtests/nested/中文 文件.py",
    }

    f1.write_text("value = 2\n", encoding="utf-8")
    second = source_snapshot.compute_source_snapshot_details(repo)
    assert second.verified is True
    assert second.value != first.value


def test_generated_docs_output_does_not_change_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "docs" / "progress" / "runs").mkdir(parents=True)
    shot = repo / "docs" / "progress" / "runs" / "preview.png"
    shot.write_bytes(b"one")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    first = source_snapshot.compute_source_snapshot_details(repo)
    shot.write_bytes(b"two")
    second = source_snapshot.compute_source_snapshot_details(repo)
    assert second.value == first.value
