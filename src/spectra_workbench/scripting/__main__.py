"""CLI 入口：允许脚本引擎会话独立启动（P7-02）。

用法::

    python -m spectra_workbench.scripting <engine> <session_dir> <script.py>
"""

from __future__ import annotations

import sys

from spectra_workbench.scripting.session import (
    SESSION_ENGINES,
    ScriptSession,
    SessionError,
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print(
            "用法：python -m spectra_workbench.scripting "
            f"<engine∈{SESSION_ENGINES}> <session_dir> <script.py>"
        )
        return 2
    engine, session_dir, script_path = argv
    try:
        session = ScriptSession(engine, session_dir=session_dir)
        with open(script_path, encoding="utf-8-sig") as fh:
            payload = session.run(fh.read())
    except SessionError as exc:
        print(f"SessionError: {exc}")
        return 3
    print(
        "session {sid} engine={eng} exit={code} artifacts={n}".format(
            sid=payload["session_id"],
            eng=payload["engine"],
            code=payload["exit_code"],
            n=len(payload["artifacts"]),
        )
    )
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
