"""Compute a reproducible source snapshot identifier for test-run evidence.

The identifier combines:
- git HEAD commit;
- dirty flag from ``git status --porcelain``;
- SHA-256 of the binary diff against HEAD;
- every untracked file's normalized path, length and content hash, obtained
  with ``git ls-files --others --exclude-standard -z`` (NUL-delimited, so
  Chinese/space paths are not parsed from quoted porcelain text).

A read failure makes the snapshot explicitly unverified instead of silently
hashing an error string. Callers that use the snapshot as a release gate must
check ``verified``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceSnapshot:
    value: str
    verified: bool
    head: str = ""
    dirty: bool = False
    diff_sha256: str = ""
    untracked_count: int = 0
    untracked_sha256: str = ""
    errors: list[str] = field(default_factory=list)
    untracked_files: list[dict[str, object]] = field(default_factory=list)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        stderr=subprocess.DEVNULL,
    )


def compute_source_snapshot_details(root: Path | None = None) -> SourceSnapshot:
    root = (root or Path.cwd()).resolve()
    errors: list[str] = []
    try:
        head = _git(root, "rev-parse", "HEAD").decode("ascii", "replace").strip()
        status = _git(
            root,
            "status",
            "--porcelain=v1",
            "--",
            ".",
            ":(exclude)docs/progress/runs/**",
            ":(exclude)work/**",
        ).decode("utf-8", "replace")
        diff = _git(
            root,
            "diff",
            "--binary",
            "HEAD",
            "--",
            ".",
            ":(exclude)docs/progress/runs/**",
            ":(exclude)work/**",
        )
        untracked_raw = _git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            ":(exclude)docs/progress/runs/**",
            ":(exclude)work/**",
        )
    except Exception as exc:
        msg = f"git_error:{type(exc).__name__}:{exc}"
        return SourceSnapshot(value=f"unknown({msg})", verified=False, errors=[msg])

    diff_sha = hashlib.sha256(diff).hexdigest()
    untracked_paths: list[str] = []
    for raw in untracked_raw.split(b"\0"):
        if not raw:
            continue
        # git -z returns raw path bytes; fsdecode preserves Chinese/space paths.
        rel = os.fsdecode(raw)
        rel = rel.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        untracked_paths.append(rel)
    untracked_paths.sort()

    untracked_hash = hashlib.sha256()
    untracked_files: list[dict[str, object]] = []
    for rel in untracked_paths:
        p = root / rel
        record: dict[str, object] = {"path": rel, "size": None, "sha256": None}
        try:
            data = p.read_bytes()
        except Exception as exc:
            errors.append(f"untracked_read_failed:{rel}:{type(exc).__name__}:{exc}")
            untracked_hash.update(rel.encode("utf-8", "surrogateescape"))
            untracked_hash.update(b"\0<unreadable>\0")
            record["error"] = f"{type(exc).__name__}: {exc}"
            untracked_files.append(record)
            continue
        digest = hashlib.sha256(data).hexdigest()
        record["size"] = len(data)
        record["sha256"] = digest
        untracked_files.append(record)
        untracked_hash.update(rel.encode("utf-8", "surrogateescape"))
        untracked_hash.update(b"\0")
        untracked_hash.update(str(len(data)).encode("ascii"))
        untracked_hash.update(b"\0")
        untracked_hash.update(data)
        untracked_hash.update(b"\0")

    verified = not errors
    value = (
        f"git:{head}:{'dirty' if status.strip() else 'clean'}:"
        f"diff={diff_sha[:16]}:untracked={len(untracked_paths)}:"
        f"{untracked_hash.hexdigest()[:16]}:verified={1 if verified else 0}"
    )
    return SourceSnapshot(
        value=value,
        verified=verified,
        head=head,
        dirty=bool(status.strip()),
        diff_sha256=diff_sha,
        untracked_count=len(untracked_paths),
        untracked_sha256=untracked_hash.hexdigest(),
        errors=errors,
        untracked_files=untracked_files,
    )


def compute_source_snapshot(root: Path | None = None) -> str:
    """Backward-compatible string form; see details for verification status."""
    return compute_source_snapshot_details(root).value


if __name__ == "__main__":
    detail = compute_source_snapshot_details()
    print(detail.value)
    if not detail.verified:
        raise SystemExit(2)
