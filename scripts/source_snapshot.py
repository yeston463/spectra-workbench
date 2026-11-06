"""Compute a reproducible source snapshot identifier for test-run evidence.

The identifier combines:
- git HEAD commit;
- dirty flag from ``git status --porcelain``;
- SHA-256 of the binary diff against HEAD;
- count and content hash of untracked files.

It intentionally does not include the work/ directory: test runs and dumps live
there and must not change the source identity.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def compute_source_snapshot(root: Path | None = None) -> str:
    root = (root or Path.cwd()).resolve()
    try:
        head = _git(root, "rev-parse", "HEAD").decode("ascii", "replace").strip()
        status = _git(root, "status", "--porcelain=v1").decode("utf-8", "replace")
        diff = _git(root, "diff", "--binary", "HEAD")
    except Exception as exc:
        return f"unknown(git-error:{type(exc).__name__})"

    diff_hash = hashlib.sha256(diff).hexdigest()[:16]
    untracked = [
        line[3:].strip()
        for line in status.splitlines()
        if line.startswith("?? ")
    ]
    untracked_hash = hashlib.sha256()
    for rel in sorted(untracked):
        p = root / rel
        untracked_hash.update(rel.encode("utf-8", "replace"))
        try:
            untracked_hash.update(p.read_bytes())
        except Exception as exc:
            untracked_hash.update(f"<unreadable:{type(exc).__name__}>".encode())
    dirty = "dirty" if status.strip() else "clean"
    return (
        f"git:{head}:{dirty}:diff={diff_hash}:"
        f"untracked={len(untracked)}:{untracked_hash.hexdigest()[:16]}"
    )


if __name__ == "__main__":
    print(compute_source_snapshot())
