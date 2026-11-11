"""成果 Staging 提交清单与故障恢复管理器（P1-05）。

契约保证：
1. 阶段持久化：在 staging 目录下持久化 manifest.json，记录每一步状态变迁（staging -> committing -> committed / failed）。
2. 多文件与 Sidecar 原子清单：记录每个主文件与伴生文件的精确目标映射与哈希。
3. 异常与断电恢复：若 Worker 进程被强杀（kill）或中途崩溃，恢复机制根据 manifest.json：
   - 区分成果是否处于未闭合中间态；
   - 撤销部分已写目标，恢复被覆盖的原目标备份文件；
   - 彻底防止残缺文件被错误识别或登记为 succeeded 图层。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def hash_file(path: Path) -> str:
    """计算文件的 SHA-256 哈希值。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入 JSON 文件（先写同目录临时文件 + 刷新落盘 + os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".tmp_{path.name}_{os.getpid()}_{int(time.monotonic() * 1000)}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


@dataclass
class StagingManifest:
    """Staging 成果提交清单持久化模型。"""

    manifest_version: str = "1.0"
    task_id: str = ""
    attempt: int = 1
    capability_id: str = ""
    state: str = "staging"  # staging | verifying | committing | committed | failed | interrupted | recovery_failed | recovered
    created_at_utc: str = field(default_factory=_utc_now_iso)
    updated_at_utc: str = field(default_factory=_utc_now_iso)
    planned_outputs: list[dict[str, Any]] = field(default_factory=list)
    staged_files: list[dict[str, Any]] = field(
        default_factory=list
    )  # {"path": ..., "size": ..., "sha256": ...}
    commit_pairs: list[dict[str, str]] = field(
        default_factory=list
    )  # {"staged": ..., "target": ...}
    backups: list[dict[str, str]] = field(default_factory=list)  # {"target": ..., "backup": ...}
    file_entries: list[dict[str, Any]] = field(default_factory=list)  # 逐文件 WAL 意图与恢复状态
    committed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    recovery_notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagingManifest:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def save_manifest(staging_dir: Path, manifest: StagingManifest) -> Path:
    """将清单保存至 staging_dir / manifest.json。"""
    manifest.updated_at_utc = _utc_now_iso()
    manifest_path = staging_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest.to_dict())
    return manifest_path


def load_manifest(staging_dir: Path) -> StagingManifest | None:
    """从 staging_dir 读取 manifest.json，若不存在返回 None。"""
    manifest_path = staging_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return StagingManifest.from_dict(data)
    except Exception:
        return None


def recover_staging(staging_dir: Path) -> dict[str, Any]:
    """对意外中断、强杀或崩溃后的 staging 目录执行恢复。

    契约与安全保证：
    1. 绝不误删未备份、未触碰的原文件：无备份记录且无证据表明目标由本事务安装时，严禁删除已有文件。
    2. 绝不删除唯一备份直至 commit 标记落盘：备份还原必须可重放、幂等。
    3. 异常不吞：恢复操作失败（如 PermissionError）时，保留 staging 和备份，标记为 recovery_failed，
       绝不盲目删除 staged 文件，绝不把失败包装为 recovered。
    4. 只有全部目标安全还原、未完成半成品彻底清理后，才删除 staging 计算文件并标为 recovered。
    """
    manifest = load_manifest(staging_dir)
    if manifest is None:
        cleaned_files = []
        if staging_dir.is_dir():
            for f in staging_dir.iterdir():
                if f.is_file():
                    with contextlib.suppress(Exception):
                        f.unlink()
                        cleaned_files.append(f.name)
        return {"recovered": True, "action": "cleaned_dangling_dir", "files": cleaned_files}

    if manifest.state in {"committed", "cleanup_pending"}:
        for b_info in manifest.backups:
            committed_bak = Path(b_info.get("backup", ""))
            if committed_bak.exists():
                with contextlib.suppress(Exception):
                    committed_bak.unlink()
        return {
            "recovered": True,
            "action": "already_committed",
            "task_id": manifest.task_id,
            "attempt": manifest.attempt,
            "committed_files": manifest.committed_files,
            "deleted_files": manifest.deleted_files,
        }

    if manifest.state == "recovered":
        return {
            "recovered": True,
            "action": "already_recovered",
            "task_id": manifest.task_id,
            "attempt": manifest.attempt,
            "recovery_notes": manifest.recovery_notes,
        }

    restored_backups: list[str] = []
    removed_partial_targets: list[str] = []
    preserved_originals: list[str] = []
    errors: list[str] = []

    # 1. 优先使用精细 WAL 记录（file_entries）执行可重放恢复
    if manifest.file_entries:
        for entry in manifest.file_entries:
            target_p = Path(str(entry["target_path"]))
            bak_p: Path | None = (
                Path(str(entry["backup_path"])) if entry.get("backup_path") else None
            )
            initial_exists = entry.get("initial_exists", False)
            status = entry.get("status", "planned")

            if initial_exists:
                # 目标原本存在：核心保证用户原文件绝不被删除
                if bak_p and bak_p.exists():
                    try:
                        os.replace(bak_p, target_p)
                        restored_backups.append(str(target_p))
                    except Exception as exc:
                        errors.append(f"Failed to restore backup {bak_p} to {target_p}: {exc}")
                elif target_p.exists():
                    # 备份不存在，但目标存在：判定是否为未触碰的原文件
                    current_sha = None
                    with contextlib.suppress(Exception):
                        current_sha = hash_file(target_p)
                    init_sha = entry.get("initial_sha256")
                    staged_sha = entry.get("staged_sha256")

                    if init_sha and current_sha == init_sha:
                        # 哈希一致，确系原文件未被改动
                        preserved_originals.append(str(target_p))
                    elif status == "planned":
                        # 尚处于计划阶段，原文件未被触碰，严禁删除
                        preserved_originals.append(str(target_p))
                    elif status == "installed" or (staged_sha and current_sha == staged_sha):
                        # 已被新文件覆盖但备份未找到：保留当前目标防止数据全无，报告错误
                        errors.append(
                            f"Target {target_p} was replaced but backup is missing. Retained to prevent data loss."
                        )
                    else:
                        preserved_originals.append(str(target_p))
                elif status in {"backed_up", "deleted"}:
                    # 原目标已被移走，备份却不在：不能假装恢复成功。
                    errors.append(f"Target {target_p} was backed up/deleted but backup is missing.")
            else:
                # 目标原本不存在（本次任务全新创建）：
                # 严格校验：仅当目标确实存在，且其哈希与本次事务暂存内容确切吻合时，才允许删除半成品
                if target_p.exists():
                    staged_sha = entry.get("staged_sha256")
                    current_sha = None
                    with contextlib.suppress(Exception):
                        current_sha = hash_file(target_p)

                    if staged_sha and current_sha == staged_sha:
                        try:
                            target_p.unlink()
                            removed_partial_targets.append(str(target_p))
                        except Exception as exc:
                            errors.append(f"Failed to remove partial target {target_p}: {exc}")
                    else:
                        # 哈希不匹配，可能系后续独立新建的无关文件，严禁删除
                        preserved_originals.append(str(target_p))
    else:
        # 兼容旧格式（无 file_entries 时）
        # 1.1 还原已知备份
        backed_up_targets: set[str] = set()
        for b in reversed(manifest.backups):
            target_p = Path(b["target"])
            bak_p = Path(b["backup"])
            if bak_p.exists():
                try:
                    os.replace(bak_p, target_p)
                    restored_backups.append(str(target_p))
                    backed_up_targets.add(str(target_p))
                except Exception as exc:
                    errors.append(f"Failed to restore backup {bak_p} to {target_p}: {exc}")
            elif target_p.exists():
                backed_up_targets.add(str(target_p))

        # 1.2 对于没有备份记录的目标：
        # 若 manifest.backups 为空且未有 committed_files 记录，说明物理备份尚未开始，
        # 现有目标是原文件，绝对禁止删除！
        for pair in manifest.commit_pairs:
            target_p = Path(pair["target"])
            if str(target_p) not in backed_up_targets and target_p.exists():
                if str(target_p) in manifest.committed_files:
                    try:
                        target_p.unlink()
                        removed_partial_targets.append(str(target_p))
                    except Exception as exc:
                        errors.append(f"Failed to remove partial target {target_p}: {exc}")
                else:
                    preserved_originals.append(str(target_p))

    # 3. 错误处理与状态持久化
    if errors:
        # 恢复失败：保留 staging 文件和备份，标记为 recovery_failed，绝不吞错
        manifest.state = "recovery_failed"
        manifest.error = {"code": "recovery_failed", "details": errors}
        manifest.recovery_notes = (
            f"Recovery failed with {len(errors)} error(s). Staging and backups preserved for retry."
        )
        save_manifest(staging_dir, manifest)
        return {
            "recovered": False,
            "action": "recovery_failed",
            "task_id": manifest.task_id,
            "attempt": manifest.attempt,
            "errors": errors,
            "restored_backups": restored_backups,
            "removed_partial_targets": removed_partial_targets,
            "preserved_originals": preserved_originals,
        }

    # 4. 全部恢复成功：清理 staging 临时计算文件（保留 manifest.json）
    for sf in manifest.staged_files:
        p = Path(sf.get("path", ""))
        if p.exists() and p != (staging_dir / "manifest.json"):
            with contextlib.suppress(Exception):
                p.unlink()

    manifest.state = "recovered"
    manifest.recovery_notes = (
        f"Recovered / reverted uncommitted state: restored {len(restored_backups)} backups, "
        f"removed {len(removed_partial_targets)} partial targets, "
        f"preserved {len(preserved_originals)} original targets."
    )
    save_manifest(staging_dir, manifest)

    return {
        "recovered": True,
        "action": "reverted_uncommitted_state",
        "task_id": manifest.task_id,
        "attempt": manifest.attempt,
        "restored_backups": restored_backups,
        "removed_partial_targets": removed_partial_targets,
        "preserved_originals": preserved_originals,
    }
