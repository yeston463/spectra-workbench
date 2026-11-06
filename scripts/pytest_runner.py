"""Run pytest and write an explicit exit-mode result before finalization.

Default mode is ``clean``: ``pytest.main()`` returns normally and Python's
finalization is allowed to run. The outer supervisor
(``scripts/run_with_timeout.py``) is the only authority that can mark
``clean_exit_verified=true`` after it observes the real OS exit code.

``bypass`` is an explicit temporary mode. It calls ``os._exit`` only after
``pytest.main()`` has returned and after the preliminary JSON has been flushed.
Bypass result always carries ``bypass_finalization=true`` and
``clean_exit_verified=false``; it is not a release gate.

This file deliberately contains no Qt imports. It is launched by the UI venv
because pytest and the test conftest need it, but the runner itself is generic.
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

# When executed as ``python scripts/pytest_runner.py``, the script directory is
# sys.path[0]; when imported by tests, fall back to the same directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from source_snapshot import compute_source_snapshot  # noqa: E402


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
        # 不认识的参数交给 pytest（便于直接复用 pytest 命令行）
        break
    if mode not in {"clean", "bypass"}:
        raise SystemExit("--mode 只能是 clean 或 bypass")
    return mode, result_json, run_id, snapshot, argv[i:]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode, result_json, run_id, snapshot, pytest_args = _parse(args)
    run_id = run_id or uuid.uuid4().hex
    snapshot = snapshot or compute_source_snapshot(Path.cwd())
    started = _utc_now()
    started_mono = time.monotonic()
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "source_snapshot": snapshot,
        "exit_mode": mode,
        "bypass_finalization": mode == "bypass",
        "pytest_exit_code": None,
        "child_exit_code": None,
        "clean_exit_verified": False,
        "cleanup_errors": [],
        "residual_processes": [],
        "started_at": started,
        "finished_at": None,
        "runner_error": None,
        "command": [sys.executable, "-m", "pytest", *pytest_args],
    }

    if result_json is not None:
        _atomic_write_json(result_json, result)

    if mode == "bypass":
        sys.stderr.write(
            "[exit-mode] BYPASS finalization requested: "
            "clean_exit_verified will remain false; not release eligible\n"
        )
        sys.stderr.flush()

    rc: int
    try:
        import pytest

        rc = int(pytest.main(pytest_args))
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except KeyboardInterrupt:
        rc = 130
        result["runner_error"] = "KeyboardInterrupt"
    except Exception as exc:  # pytest import/collection/plugin failure
        rc = 4
        result["runner_error"] = f"{type(exc).__name__}: {exc}"

    result["pytest_exit_code"] = rc
    result["finished_at"] = _utc_now()
    result["duration_seconds"] = round(time.monotonic() - started_mono, 4)
    if result_json is not None:
        _atomic_write_json(result_json, result)

    if mode == "bypass":
        # 先落盘，再强制退出；不经过解释器 finalization。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
