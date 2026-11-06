"""Run pytest and write a child result for the outer supervisor.

The supervisor (``scripts/run_with_timeout.py``) owns the run identity:
``SW_RUN_ID``, ``SW_SOURCE_SNAPSHOT``, ``SW_SOURCE_SNAPSHOT_VERIFIED``,
``SW_EXPECTED_EXIT_MODE`` and ``SW_CLEANUP_REPORT`` are injected into the child
environment. A command-line identity that disagrees with the environment is a
hard failure, never silently overridden.

Default mode is ``clean``: ``pytest.main()`` returns normally and Python's
finalization is allowed to run. ``bypass`` calls ``os._exit`` only after
``pytest.main()`` has returned and the child result has been flushed. Bypass
always writes ``bypass_finalization=true`` and ``clean_exit_verified=false``.

The child file (preliminary result) and supervisor final file must be different
paths. The supervisor validates run_id, source snapshot, mode, completion and
cleanup status before it can mark a run clean.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from source_snapshot import compute_source_snapshot_details  # noqa: E402

EXIT_RESULT_INVALID = 92


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp_{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _parse(argv: list[str]) -> tuple[str, Path | None, str, str | None, list[str]]:
    mode = "clean"
    result_json: Path | None = None
    run_id = ""
    snapshot: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg in ("--mode", "--result-json", "--run-id", "--source-snapshot"):
            if i + 1 >= len(argv):
                raise SystemExit(f"{arg} 缺少参数值")
            value = argv[i + 1]
            if arg == "--mode":
                mode = value
            elif arg == "--result-json":
                result_json = Path(value)
            elif arg == "--run-id":
                run_id = value
            else:
                snapshot = value
            i += 2
            continue
        break
    if mode not in {"clean", "bypass"}:
        raise SystemExit("--mode 只能是 clean 或 bypass")
    return mode, result_json, run_id, snapshot, argv[i:]


def _cleanup_state() -> tuple[bool, list[str], Path | None]:
    path_text = os.environ.get("SW_CLEANUP_REPORT")
    if not path_text:
        return False, ["cleanup_report_not_configured"], None
    path = Path(path_text)
    if not path.exists():
        return False, ["cleanup_report_missing"], path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"cleanup_report_invalid: {type(exc).__name__}: {exc}"], path
    if not isinstance(data, dict) or data.get("schema_version") != "1.0":
        return False, ["cleanup_report_invalid_schema"], path
    errors = data.get("errors")
    if not isinstance(errors, list):
        errors = ["cleanup_report_errors_not_list"]
    complete = data.get("complete") is True
    return complete, [str(e) for e in errors], path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cli_mode, result_json, cli_run_id, cli_snapshot, pytest_args = _parse(args)

    env_mode = os.environ.get("SW_EXPECTED_EXIT_MODE")
    env_run_id = os.environ.get("SW_RUN_ID")
    env_snapshot = os.environ.get("SW_SOURCE_SNAPSHOT")
    env_snapshot_verified = os.environ.get("SW_SOURCE_SNAPSHOT_VERIFIED") == "1"

    identity_errors: list[str] = []
    expected_mode = env_mode or cli_mode
    if env_mode and env_mode != cli_mode:
        identity_errors.append(
            f"mode_mismatch: env={env_mode!r} cli={cli_mode!r}"
        )
    if env_run_id and cli_run_id and env_run_id != cli_run_id:
        identity_errors.append(
            f"run_id_mismatch: env={env_run_id!r} cli={cli_run_id!r}"
        )
    run_id = env_run_id or cli_run_id or uuid.uuid4().hex
    snapshot = env_snapshot or cli_snapshot
    snapshot_verified = env_snapshot_verified if env_snapshot else False
    if snapshot is None:
        detail = compute_source_snapshot_details(Path.cwd())
        snapshot = detail.value
        snapshot_verified = detail.verified

    started = _utc_now()
    started_mono = time.monotonic()
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "source_snapshot": snapshot,
        "source_snapshot_verified": bool(snapshot_verified),
        "exit_mode": cli_mode,
        "expected_exit_mode": expected_mode,
        "bypass_finalization": cli_mode == "bypass",
        "pytest_exit_code": None,
        "child_exit_code": None,
        "clean_exit_verified": False,
        "cleanup_check_complete": False,
        "cleanup_errors": [],
        "residual_processes": [],
        "pytest_was_run": False,
        "completed": False,
        "started_at": started,
        "finished_at": None,
        "runner_error": None,
        "identity_errors": identity_errors,
        "command": [sys.executable, "-m", "pytest", *pytest_args],
    }

    if identity_errors:
        result["runner_error"] = "; ".join(identity_errors)
        result["pytest_exit_code"] = EXIT_RESULT_INVALID
        result["completed"] = True
        result["finished_at"] = _utc_now()
        if result_json is not None:
            _atomic_write_json(result_json, result)
        if cli_mode == "bypass":
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(EXIT_RESULT_INVALID)
        return EXIT_RESULT_INVALID

    if result_json is not None:
        _atomic_write_json(result_json, result)

    if cli_mode == "bypass":
        sys.stderr.write(
            "[exit-mode] BYPASS finalization requested: "
            "clean_exit_verified will remain false; not release eligible\n"
        )
        sys.stderr.flush()

    rc: int
    pytest_ran = False
    try:
        import pytest

        rc = int(pytest.main(pytest_args))
        pytest_ran = True
    except SystemExit as exc:
        rc = int(exc.code or 0)
        pytest_ran = True
    except KeyboardInterrupt:
        rc = 130
        result["runner_error"] = "KeyboardInterrupt"
    except Exception as exc:  # pytest import/collection/plugin failure
        rc = 4
        result["runner_error"] = f"{type(exc).__name__}: {exc}"

    cleanup_complete, cleanup_errors, cleanup_path = _cleanup_state()
    result["pytest_exit_code"] = rc
    result["pytest_was_run"] = pytest_ran
    result["completed"] = bool(pytest_ran and result["runner_error"] is None)
    result["cleanup_check_complete"] = cleanup_complete
    result["cleanup_errors"] = cleanup_errors
    if cleanup_path is not None:
        result["cleanup_report"] = str(cleanup_path)
    result["finished_at"] = _utc_now() if result["completed"] else None
    result["duration_seconds"] = round(time.monotonic() - started_mono, 4)
    if result_json is not None:
        _atomic_write_json(result_json, result)

    if cli_mode == "bypass":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
