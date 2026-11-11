"""插件管理器与生命周期状态机（P8-04）。

负责：
1. 插件安装（支持解包与目录安装）、卸载与持久化登记
2. 插件发现（本地目录扫描与 Python entry points，GUI 进程零原生导入）
3. 状态机：discovered / enabled / disabled / incompatible / failed
4. 动态能力注册与注销（对接 CapabilityRegistry）
5. 崩溃隔离与连续失败熔断恢复机制（连续失败 >= 3 次自动隔离禁用并提供重置入口）
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import tempfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spectra_workbench.plugins.manifest import (
    PLATFORM_API_VERSION,
    PLATFORM_VERSION,
    PluginInfo,
    PluginManifest,
    PluginStatus,
)
from spectra_workbench.registry.catalog import CapabilityRegistry


def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:
        u = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        return Path(u) if u else Path(tempfile.gettempdir())


class PluginError(Exception):
    """插件管理异常。"""

    pass


class PluginManager:
    """插件管理器。"""

    DEFAULT_PLUGINS_DIR = _safe_home() / ".spectra" / "plugins"
    DEFAULT_STATE_FILE = _safe_home() / ".spectra" / "plugins_state.json"

    def __init__(
        self,
        plugins_dir: Path | str | None = None,
        state_file: Path | str | None = None,
        registry: CapabilityRegistry | None = None,
        failure_threshold: int = 3,
    ) -> None:
        if plugins_dir is not None:
            self.plugins_dir = Path(plugins_dir).resolve()
        else:
            env_dir = os.environ.get("SPECTRA_PLUGINS_DIR")
            self.plugins_dir = Path(env_dir).resolve() if env_dir else self.DEFAULT_PLUGINS_DIR

        if state_file is not None:
            self.state_file = Path(state_file).resolve()
        else:
            env_state = os.environ.get("SPECTRA_PLUGINS_STATE_FILE")
            self.state_file = Path(env_state).resolve() if env_state else self.DEFAULT_STATE_FILE

        self.registry = registry
        self.failure_threshold = max(1, failure_threshold)
        self._lock = threading.Lock()
        self._plugins: dict[str, PluginInfo] = {}

    def _load_persisted_state(self) -> dict[str, Any]:
        """读取插件持久化状态字典。"""
        if not self.state_file.exists():
            return {"schema_version": "1.0", "plugins": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "plugins" in data:
                return data
            return {"schema_version": "1.0", "plugins": {}}
        except Exception:
            return {"schema_version": "1.0", "plugins": {}}

    def _save_persisted_state(self) -> None:
        """原子写入插件持久化状态。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "plugins": {
                p.manifest.id: {
                    "status": p.status.value,
                    "consecutive_failures": p.consecutive_failures,
                    "incompatibility_reason": p.incompatibility_reason,
                    "installed_at": p.installed_at,
                }
                for p in self._plugins.values()
            },
        }
        tmp_file = self.state_file.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, self.state_file)

    def discover(self) -> list[PluginInfo]:
        """执行全量插件发现（目录扫描 + Entry Points），不导入任何原生模块。"""
        with self._lock:
            persisted = self._load_persisted_state().get("plugins", {})
            found: dict[str, PluginInfo] = {}

            # 1. 扫描本地目录
            if self.plugins_dir.exists():
                for item in self.plugins_dir.iterdir():
                    manifest_path = None
                    if item.is_dir():
                        for cand in ("plugin.json", "manifest.json", "spectra-plugin.json"):
                            if (item / cand).is_file():
                                manifest_path = item / cand
                                break
                    if manifest_path:
                        try:
                            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                            m = PluginManifest.model_validate(raw)
                            info = PluginInfo(manifest=m, install_path=item)
                            found[m.id] = info
                        except Exception:
                            continue

            # 2. 扫描 Python Entry Points
            try:
                eps = importlib.metadata.entry_points(group="spectra_workbench.plugins")
                for ep in eps:
                    ep_id = ep.name
                    if ep_id not in found:
                        m = PluginManifest(
                            id=ep_id,
                            name=ep.name,
                            version="1.0.0",
                            entry_point=ep.value,
                            description=f"Entry point plugin: {ep.name}",
                        )
                        found[ep_id] = PluginInfo(manifest=m, install_path=None)
            except Exception:
                pass

            # 3. 合并持久化状态、检查版本兼容性与失败熔断
            for pid, info in found.items():
                m = info.manifest
                compat, reason = m.check_compatibility(PLATFORM_API_VERSION, PLATFORM_VERSION)
                stored = persisted.get(pid, {})
                consecutive_fails = int(stored.get("consecutive_failures", 0))
                info.consecutive_failures = consecutive_fails

                if not compat:
                    info.status = PluginStatus.incompatible
                    info.incompatibility_reason = reason
                elif consecutive_fails >= self.failure_threshold:
                    info.status = PluginStatus.failed
                    info.incompatibility_reason = f"连续崩溃/失败 {consecutive_fails} 次（达到阈值 {self.failure_threshold}），已自动隔离禁用"
                else:
                    stored_status = stored.get("status")
                    if stored_status in (PluginStatus.disabled.value, PluginStatus.disabled):
                        info.status = PluginStatus.disabled
                    else:
                        info.status = PluginStatus.enabled

                # 4. 同步能力至 CapabilityRegistry
                if self.registry is not None:
                    if info.status == PluginStatus.enabled:
                        for cap in m.capabilities:
                            desc = cap.to_descriptor(m)
                            self.registry.register(desc)
                    else:
                        for cap in m.capabilities:
                            self.registry.unregister(cap.capability_id)

            self._plugins = found
            self._save_persisted_state()
            return sorted(list(self._plugins.values()), key=lambda p: p.manifest.id)

    def install_plugin(self, source_path: Path | str) -> PluginInfo:
        """安装插件（支持解压 .zip 压缩包或拷贝插件目录）。"""
        src = Path(source_path).resolve()
        if not src.exists():
            raise PluginError(f"插件安装源不存在: {src}")

        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        target_dir: Path | None = None
        manifest_raw: dict[str, Any] | None = None

        if src.is_dir():
            for cand in ("plugin.json", "manifest.json", "spectra-plugin.json"):
                if (src / cand).is_file():
                    manifest_raw = json.loads((src / cand).read_text(encoding="utf-8"))
                    break
            if not manifest_raw:
                raise PluginError(f"目录中未找到 plugin.json 清单文件: {src}")
            m = PluginManifest.model_validate(manifest_raw)
            target_dir = self.plugins_dir / m.id
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.copytree(src, target_dir)

        elif src.suffix.lower() in (".zip", ".bundle"):
            with zipfile.ZipFile(src, "r") as zf:
                json_name = next(
                    (
                        n
                        for n in zf.namelist()
                        if Path(n).name in ("plugin.json", "manifest.json", "spectra-plugin.json")
                    ),
                    None,
                )
                if not json_name:
                    raise PluginError(f"压缩包中未找到 plugin.json: {src}")
                manifest_raw = json.loads(zf.read(json_name).decode("utf-8"))
                m = PluginManifest.model_validate(manifest_raw)
                target_dir = self.plugins_dir / m.id
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)
                target_dir.mkdir(parents=True, exist_ok=True)

                # 若压缩包包含顶层目录包裹，剥离该前缀使 plugin.json 位于 target_dir 根部
                prefix = str(Path(json_name).parent).replace("\\", "/")
                if prefix and prefix != ".":
                    prefix_with_slash = prefix if prefix.endswith("/") else f"{prefix}/"
                    for member in zf.namelist():
                        if member.startswith(prefix_with_slash):
                            rel_name = member[len(prefix_with_slash) :]
                            if not rel_name:
                                continue
                            dest = target_dir / rel_name
                            if member.endswith("/"):
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(zf.read(member))
                else:
                    zf.extractall(target_dir)
        else:
            raise PluginError(f"不支持的插件格式: {src.suffix}")

        self.discover()
        info = self.get_plugin(m.id)
        if info is None:
            raise PluginError(f"安装后未能成功发现插件: {m.id}")
        return info

    def uninstall_plugin(self, plugin_id: str) -> bool:
        """卸载插件并从注册表与状态中清除。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                return False

            if self.registry is not None:
                for cap in info.manifest.capabilities:
                    self.registry.unregister(cap.capability_id)

            if info.install_path and info.install_path.exists():
                shutil.rmtree(info.install_path, ignore_errors=True)

            del self._plugins[plugin_id]
            self._save_persisted_state()
            return True

    def enable_plugin(self, plugin_id: str) -> None:
        """启用插件并注册其能力。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                raise PluginError(f"插件不存在: {plugin_id}")
            if info.status == PluginStatus.incompatible:
                raise PluginError(f"插件版本不兼容，无法启用: {info.incompatibility_reason}")
            if info.status == PluginStatus.failed:
                raise PluginError(
                    f"插件处于失败隔离状态，请先调用 reset_plugin_failure 恢复: {info.incompatibility_reason}"
                )

            info.status = PluginStatus.enabled
            if self.registry is not None:
                for cap in info.manifest.capabilities:
                    desc = cap.to_descriptor(info.manifest)
                    self.registry.register(desc)
            self._save_persisted_state()

    def disable_plugin(self, plugin_id: str) -> None:
        """禁用插件并注销其能力。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                raise PluginError(f"插件不存在: {plugin_id}")

            info.status = PluginStatus.disabled
            if self.registry is not None:
                for cap in info.manifest.capabilities:
                    self.registry.unregister(cap.capability_id)
            self._save_persisted_state()

    def record_failure(self, plugin_id: str, error_message: str | None = None) -> None:
        """记录插件任务执行失败。若连续失败达到阈值则触发自动熔断隔离。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                return
            info.consecutive_failures += 1
            if info.consecutive_failures >= self.failure_threshold:
                info.status = PluginStatus.failed
                info.incompatibility_reason = f"连续崩溃/失败 {info.consecutive_failures} 次（达到阈值 {self.failure_threshold}），已自动熔断隔离"
                if self.registry is not None:
                    for cap in info.manifest.capabilities:
                        self.registry.unregister(cap.capability_id)
            self._save_persisted_state()

    def record_success(self, plugin_id: str) -> None:
        """记录插件任务执行成功，清零连续失败计数器。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                return
            info.consecutive_failures = 0
            self._save_persisted_state()

    def reset_plugin_failure(self, plugin_id: str) -> None:
        """重置插件失败计数器并恢复启用状态（崩溃恢复入口）。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            if info is None:
                raise PluginError(f"插件不存在: {plugin_id}")

            compat, reason = info.manifest.check_compatibility(
                PLATFORM_API_VERSION, PLATFORM_VERSION
            )
            info.consecutive_failures = 0
            if not compat:
                info.status = PluginStatus.incompatible
                info.incompatibility_reason = reason
            else:
                info.status = PluginStatus.enabled
                info.incompatibility_reason = None
                if self.registry is not None:
                    for cap in info.manifest.capabilities:
                        desc = cap.to_descriptor(info.manifest)
                        self.registry.register(desc)
            self._save_persisted_state()

    def get_plugin(self, plugin_id: str) -> PluginInfo | None:
        """获取指定插件信息。"""
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[PluginInfo]:
        """获取当前所有已知插件列表。"""
        return sorted(list(self._plugins.values()), key=lambda p: p.manifest.id)
