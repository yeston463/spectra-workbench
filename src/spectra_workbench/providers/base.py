"""Provider 适配器基类、执行上下文与持久化提交清单契约（P1-03 / P1-05）。

定义引擎适配器标准接口、路径解析器与任务执行上下文。所有引擎工作进程（GDAL / Rasterio / Science）
均通过实现 ProviderAdapter 接入平台统一调度管线。
包含成果提交清单（manifest.json）持久化、跨卷安全提交与异常回滚机制。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from spectra_workbench.domain.models import CapabilityDescriptor, TaskResult, TaskSpec
from spectra_workbench.domain.paths import (
    is_archive,
    is_vsi_or_remote,
    normalize_vsi_path,
)
from spectra_workbench.processing.manifest import (
    StagingManifest,
    hash_file,
    save_manifest,
)


class TaskCancelledError(Exception):
    """任务被用户或调度器取消。"""


class OutputConflictError(Exception):
    """目标成果已存在且未声明 overwrite=True。"""


class ProtectedInputError(OutputConflictError):
    """提交计划试图删除或覆盖受保护的输入文件。"""


class InputOutputConflictError(Exception):
    """输出成果与输入源指向同一文件。"""


class CommitTransactionError(Exception):
    """成果提交事务失败。"""


def resolve_dataset_path(uri_or_path: str, project_root: Path | str) -> Path:
    """统一解析 DatasetRef / OutputPlan / Workspace 路径为本地绝对 Path。

    契约保证：
    1. file:// URI：经 urlparse + url2pathname 解析为本地绝对路径。
    2. 本地绝对路径（Windows 盘符如 C:\\...、UNC 路径 \\\\server\\share\\...、POSIX /...）：直接返回 resolve()。
    3. 相对路径（如 "outputs/result.tif" 或 "./data/in.dat"）：严格以 project_root 为基准解析为绝对路径：
       (Path(project_root) / uri_or_path).resolve()
    4. 无论当前进程工作目录 (cwd) 为何，解析结果严格确定且一致，禁止受子进程 cwd 干扰。
    """
    if uri_or_path.startswith("file:"):
        parsed = urlparse(uri_or_path)
        return Path(url2pathname(parsed.path)).resolve()
    p = Path(uri_or_path)
    if p.is_absolute():
        return p.resolve()
    return (Path(project_root) / p).resolve()


def resolve_input_resource(uri_or_path: str, project_root: Path | str) -> Path | str:
    """解析输入资源路径：若是 VSI / 远程 / 归档则返回规范化 VSI 字符串，否则返回本地绝对 Path。"""
    if is_vsi_or_remote(uri_or_path) or is_archive(uri_or_path):
        return normalize_vsi_path(uri_or_path)
    return resolve_dataset_path(uri_or_path, project_root)


@dataclass
class ExecutionContext:
    """任务执行上下文。

    向具体算法执行逻辑提供：
    1. 专有 staging 临时目录与成果提交事务（带清单持久化、冲突保护、原子替换与异常回滚）
    2. 进度上报回调
    3. 日志记录回调
    4. 协作式取消检查令牌
    5. 运行时配置字典
    """

    task_id: str
    workspace: Path
    staging_dir: Path
    is_cancelled: threading.Event = field(default_factory=threading.Event)
    progress_callback: Callable[[float | None, str, str | None], None] | None = None
    log_callback: Callable[[str, str, str | None], None] | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    manifest: StagingManifest = field(init=False)

    def __post_init__(self) -> None:
        self.manifest = StagingManifest(
            task_id=self.task_id,
            state="staging",
        )

    def progress(self, ratio: float | None, phase: str, message: str | None = None) -> None:
        """报告执行进度。ratio 为 [0.0, 1.0] 或 None（不确定进度）。"""
        if self.progress_callback:
            self.progress_callback(ratio, phase, message)

    def log(self, level: str, message: str, stream: str | None = "engine") -> None:
        """记录日志行。level 需为 DEBUG / INFO / WARNING / ERROR。脱敏敏感凭据信息。"""
        if self.log_callback:
            from spectra_workbench.domain.credentials import sanitize_text

            self.log_callback(level.upper(), sanitize_text(message), stream)

    def cancel(self) -> None:
        """置位取消标志。"""
        self.is_cancelled.set()

    def check_cancelled(self) -> None:
        """若任务已取消则抛出 TaskCancelledError，供算法中断长耗时计算。"""
        if self.is_cancelled.is_set():
            raise TaskCancelledError(f"Task {self.task_id} was cancelled by user")

    def create_staging_path(self, filename: str) -> Path:
        """在当前任务专有 staging 目录下创建文件路径并确保目录存在。"""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        return self.staging_dir / filename

    def commit_file(
        self,
        staged_path: Path,
        target_path: Path,
        overwrite: bool = False,
    ) -> Path:
        """单文件提交兼容接口，委托至 commit_files 事务执行。"""
        committed = self.commit_files([(staged_path, target_path)], overwrite=overwrite)
        return committed[0]

    def commit_files(
        self,
        pairs: list[tuple[Path, Path]],
        overwrite: bool = False,
        *,
        delete_paths: list[Path] | None = None,
        protected_paths: set[Path] | None = None,
    ) -> list[Path]:
        """将 staging 成果与可选删除动作作为一个事务提交。

        - ``pairs``：staged → target 的新成果；
        - ``delete_paths``：旧成果中本次不生成的 sidecar，先备份再删除；
        - ``protected_paths``：输入依赖，任何删除/覆盖命中都必须拒绝。

        事务顺序：预写意图 → 备份所有已有目标/待删除文件 → 安装新成果 →
        committed 决断点 → 清理备份。决断点前失败恢复完整旧集合；决断点后
        绝不反向回滚新成果。
        """
        delete_list = sorted({Path(p).resolve() for p in (delete_paths or [])})
        protected = {Path(p).resolve() for p in (protected_paths or set())}
        normalized_pairs = [(Path(s).resolve(), Path(t).resolve()) for s, t in pairs]
        if not normalized_pairs and not delete_list:
            return []

        staged_set = {s for s, _ in normalized_pairs}
        target_set = {t for _, t in normalized_pairs}
        for target in delete_list:
            if target in target_set:
                raise ValueError(f"Delete path is also a new target: {target}")
            if target in staged_set:
                raise ValueError(f"Delete path is also a staged file: {target}")
            if target in protected:
                raise ProtectedInputError(
                    f"Refusing to delete protected input file: {target}"
                )
            if target.exists() and not target.is_file():
                raise ValueError(f"Delete path is not a regular file: {target}")
        if delete_list and not overwrite:
            raise OutputConflictError(
                "Deleting stale sidecars requires overwrite=true"
            )

        # 阶段 1：静态前置冲突检查（全量，不落任何盘）
        for staged, target in normalized_pairs:
            if not staged.exists():
                raise FileNotFoundError(f"Staged file not found: {staged}")
            if target == staged:
                raise ValueError(f"Staged path and target path are identical: {target}")
            if target.exists() and not overwrite:
                raise OutputConflictError(f"Target already exists and overwrite=false: {target}")

        self.staging_dir.mkdir(parents=True, exist_ok=True)

        # 阶段 2：预检目标状态，生成操作意图与确定性备份路径（WAL Intent）
        file_entries: list[dict[str, Any]] = []
        commit_pairs: list[dict[str, Any]] = []
        staged_info: list[dict[str, Any]] = []
        backup_idx = 0

        for staged, target in normalized_pairs:
            staged_size = staged.stat().st_size
            staged_sha = hash_file(staged)
            staged_info.append({"path": str(staged), "size": staged_size, "sha256": staged_sha})
            tgt_exists = target.exists()
            init_size = target.stat().st_size if tgt_exists else None
            init_sha = hash_file(target) if tgt_exists else None
            bak_path = (
                target.with_name(f".{target.name}.bak_{self.task_id}_{backup_idx}_{uuid.uuid4().hex[:6]}")
                if tgt_exists
                else None
            )
            backup_idx += 1
            entry = {
                "staged_path": str(staged),
                "target_path": str(target),
                "action": "replace" if tgt_exists else "create",
                "initial_exists": tgt_exists,
                "initial_size": init_size,
                "initial_sha256": init_sha,
                "backup_path": str(bak_path) if bak_path else None,
                "staged_size": staged_size,
                "staged_sha256": staged_sha,
                "status": "planned",
            }
            file_entries.append(entry)
            commit_pairs.append(
                {
                    "staged": str(staged),
                    "target": str(target),
                    "action": entry["action"],
                }
            )

        for target in delete_list:
            if not target.exists():
                continue
            init_size = target.stat().st_size
            init_sha = hash_file(target)
            bak_path = target.with_name(
                f".{target.name}.bak_{self.task_id}_{backup_idx}_{uuid.uuid4().hex[:6]}"
            )
            backup_idx += 1
            entry = {
                "staged_path": None,
                "target_path": str(target),
                "action": "delete",
                "initial_exists": True,
                "initial_size": init_size,
                "initial_sha256": init_sha,
                "backup_path": str(bak_path),
                "staged_size": None,
                "staged_sha256": None,
                "status": "planned",
            }
            file_entries.append(entry)
            commit_pairs.append(
                {
                    "staged": None,
                    "target": str(target),
                    "action": "delete",
                }
            )

        # 阶段 3：预写意图日志并持久化落盘
        self.manifest.commit_pairs = commit_pairs
        self.manifest.staged_files = staged_info
        self.manifest.file_entries = file_entries
        self.manifest.backups = []
        self.manifest.committed_files = []
        self.manifest.deleted_files = []
        self.manifest.state = "committing"
        save_manifest(self.staging_dir, self.manifest)

        backups_created: list[tuple[Path, Path]] = []
        installed_files: list[tuple[Path, Path]] = []
        deleted_files: list[Path] = []

        # 阶段 4：带事务备份与安全提交执行（决断点前：可回滚）
        try:
            # 4.1 先为所有需要替换/删除的已有文件建立备份
            for entry in file_entries:
                if entry["initial_exists"] and entry["backup_path"]:
                    target_p = Path(str(entry["target_path"]))
                    backup_p = Path(str(entry["backup_path"]))
                    if target_p.exists():
                        target_p.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target_p, backup_p)
                        backups_created.append((target_p, backup_p))
                        entry["status"] = "backed_up"
                        self.manifest.backups.append(
                            {"target": str(target_p), "backup": str(backup_p)}
                        )
                        save_manifest(self.staging_dir, self.manifest)

            # 4.2 安装新成果（delete 条目只备份，不安装）
            for entry in file_entries:
                if entry["action"] == "delete":
                    continue
                staged_p = Path(str(entry["staged_path"]))
                target_p = Path(str(entry["target_path"]))
                target_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_p), str(target_p))
                installed_files.append((staged_p, target_p))
                entry["status"] = "installed"
                self.manifest.committed_files.append(str(target_p))
                save_manifest(self.staging_dir, self.manifest)

            # 阶段 5：提交决断点（Commit Point，单向不可逆）
            self.manifest.state = "committed"
            self.manifest.deleted_files = []
            for entry in file_entries:
                if entry["action"] == "delete":
                    entry["status"] = "deleted"
                    self.manifest.deleted_files.append(str(entry["target_path"]))
                    deleted_files.append(Path(str(entry["target_path"])))
                else:
                    entry["status"] = "committed"
            save_manifest(self.staging_dir, self.manifest)
        except Exception as exc:
            # 阶段 7：事务回滚（仅限提交决断前故障）
            for staged_p, target_p in reversed(installed_files):
                with contextlib.suppress(Exception):
                    if target_p.exists():
                        if not staged_p.exists():
                            shutil.move(str(target_p), str(staged_p))
                        else:
                            target_p.unlink()

            for target_p, backup_p in reversed(backups_created):
                with contextlib.suppress(Exception):
                    if backup_p.exists():
                        os.replace(backup_p, target_p)

            self.manifest.state = "failed"
            self.manifest.deleted_files = []
            self.manifest.error = {"code": "commit_failed", "message": str(exc)}
            save_manifest(self.staging_dir, self.manifest)
            raise

        # 阶段 6：提交后维护与清理（Post-Commit Cleanup，不可逆决断点已过）
        try:
            for _, bak in backups_created:
                with contextlib.suppress(Exception):
                    bak.unlink()
            self.manifest.backups.clear()
            save_manifest(self.staging_dir, self.manifest)
        except Exception as cleanup_exc:
            self.manifest.state = "cleanup_pending"
            self.manifest.error = {"code": "cleanup_failed", "message": str(cleanup_exc)}
            with contextlib.suppress(Exception):
                save_manifest(self.staging_dir, self.manifest)
            raise

        return [tgt for _, tgt in installed_files]


class ProviderAdapter(ABC):
    """各引擎 worker 内部的统一适配接口。"""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """适配器标识符（如 'gdal', 'rasterio', 'science'）。"""
        ...

    @abstractmethod
    def runtime_info(self) -> dict[str, Any]:
        """返回底层引擎运行时的详细元数据（版本、驱动、PROJ 等）。"""
        ...

    @abstractmethod
    def discover(self) -> list[CapabilityDescriptor]:
        """枚举当前适配器向平台声明的所有可用能力描述符。"""
        ...

    @abstractmethod
    def validate(self, spec: TaskSpec) -> dict[str, Any]:
        """静态校验 TaskSpec 参数与输入合法性，返回校验报告字典。"""
        ...

    @abstractmethod
    def estimate(self, spec: TaskSpec) -> dict[str, Any]:
        """估算任务资源需求（内存 MiB、临时磁盘字节数、预计耗时等）。"""
        ...

    @abstractmethod
    def execute(self, spec: TaskSpec, context: ExecutionContext) -> TaskResult:
        """执行计算任务并返回终态 TaskResult。"""
        ...

    def probe_dataset(self, path: Path | str) -> dict[str, Any]:
        """探测并返回数据集详细元数据（零全量内存加载）。"""
        raise NotImplementedError(f"{self.provider_id} 适配器未实现 probe_dataset")

    def read_window(
        self,
        path: Path | str,
        x_off: int = 0,
        y_off: int = 0,
        win_w: int = 512,
        win_h: int = 512,
        out_w: int = 512,
        out_h: int = 512,
        bands: list[int] | None = None,
        raw: bool = False,
        overview: str = "auto",
    ) -> dict[str, Any]:
        """后台分块读取局部窗口并抽样（禁止整幅读入）。

        raw=True 时返回原值窗口（base64 + 有效性位图），overview 控制概览选择。
        """
        raise NotImplementedError(f"{self.provider_id} 适配器未实现 read_window")
