"""P8-04 插件协议、生命周期与示范统计插件集成测试。

覆盖验收标准（BACKLOG P8-04 / 里程碑 A13 / 开发方案 §12）：
1. 插件 Manifest 解析、契约与版本兼容性判定（provider_api_version、min/max platform version）。
2. 插件发现与零原生依赖隔离：GUI / 发现进程不导入任何原生计算/C-extension 模块（osgeo/rasterio）。
3. 插件安装（目录与 .zip 归档）与卸载，注册表与文件系统同步清理。
4. 插件启用/禁用状态机切换，能力与 CapabilityRegistry 动态注册/注销，持久化到 plugins_state.json。
5. 不兼容插件状态标记为 incompatible 并提供明确原因，禁止越权启用。
6. 崩溃隔离与连续失败熔断机制：连续 3 次失败自动熔断隔离为 failed 并注销能力，提供 reset_plugin_failure 恢复重置。
7. 独立 Worker 进程端到端执行真实统计示范插件（sample_stats:zscore 标准化计算），验证输出 GeoTIFF 数值与全生命周期。
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.plugins.manager import PluginError, PluginManager
from spectra_workbench.plugins.manifest import (
    PLATFORM_API_VERSION,
    PluginCapabilitySpec,
    PluginManifest,
    PluginStatus,
)
from spectra_workbench.registry.catalog import CapabilityRegistry
from spectra_workbench.workers.process import WorkerProcess

pytestmark = [pytest.mark.integration]

ROOT = find_project_root()
SAMPLE_PLUGIN_SRC = ROOT / "src" / "spectra_workbench" / "plugins" / "sample"


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _make_test_geotiff(
    gdal_rt: dict[str, Any],
    path: Path,
    width: int = 64,
    height: int = 64,
) -> Path:
    """在外部 GDAL 子进程中生成测试 GeoTIFF（保证当前进程不导入 osgeo）。"""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    posix_path = path.as_posix()
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

drv = gdal.GetDriverByName("GTiff")
ds = drv.Create(r"{posix_path}", {width}, {height}, 1, gdal.GDT_Float32)
ds.SetGeoTransform([500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0])
srs = osr.SpatialReference()
srs.ImportFromEPSG(32649)
ds.SetSpatialRef(srs)

arr = np.linspace(10.0, 100.0, {width} * {height}, dtype=np.float32).reshape(({height}, {width}))
band = ds.GetRasterBand(1)
band.WriteArray(arr)
ds.FlushCache()
ds = None
print("OK")
"""
    subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return path


def _read_raster_stats(gdal_rt: dict[str, Any], path: Path) -> tuple[float, float]:
    """在外部 GDAL 子进程中读取生成结果栅格的均值与标准差。"""
    posix_path = Path(path).resolve().as_posix()
    script = f"""
import json
import numpy as np
from osgeo import gdal
gdal.UseExceptions()

ds = gdal.Open(r"{posix_path}", gdal.GA_ReadOnly)
arr = ds.GetRasterBand(1).ReadAsArray()
ds = None
mean_v = float(np.mean(arr))
std_v = float(np.std(arr))
print(json.dumps({{"mean": mean_v, "std": std_v}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    return data["mean"], data["std"]


# ---------------------------------------------------------------------------
# 1. Manifest 契约与版本兼容性测试
# ---------------------------------------------------------------------------


class TestPluginManifest:
    """验证插件清单契约模型、序列化与版本兼容性策略。"""

    def test_sample_manifest_loads_and_validates(self) -> None:
        manifest_file = SAMPLE_PLUGIN_SRC / "plugin.json"
        assert manifest_file.exists(), f"未找到示范插件清单: {manifest_file}"

        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(data)
        assert manifest.id == "spectra_plugin_sample_stats"
        assert manifest.version == "1.0.0"
        assert manifest.provider_api_version == PLATFORM_API_VERSION
        assert len(manifest.capabilities) == 1

        cap = manifest.capabilities[0]
        assert cap.capability_id == "sample_stats:zscore"
        desc = cap.to_descriptor(manifest)
        assert desc.capability_id == "sample_stats:zscore"
        assert desc.engine.provider_id == "plugin"
        assert len(desc.inputs) == 1
        assert len(desc.outputs) == 1
        assert len(desc.parameters) == 2

    def test_version_compatibility_checks(self) -> None:
        base = PluginManifest(
            id="test_plug",
            name="Test",
            version="1.0.0",
            provider_api_version="1.0",
            min_platform_version="0.1.0",
            max_platform_version="2.0.0",
        )
        # 完全匹配
        ok, reason = base.check_compatibility("1.0", "1.0.0")
        assert ok is True
        assert reason is None

        # 主版本契约不一致
        m_api = base.model_copy(update={"provider_api_version": "2.0"})
        ok, reason = m_api.check_compatibility("1.0", "1.0.0")
        assert ok is False
        assert reason is not None and "provider_api_version" in reason

        # 最低平台版本不满足
        m_min = base.model_copy(update={"min_platform_version": "2.0.0"})
        ok, reason = m_min.check_compatibility("1.0", "1.0.0")
        assert ok is False
        assert reason is not None and "minimum platform version" in reason

        # 超过最高平台版本
        m_max = base.model_copy(update={"max_platform_version": "0.9.0"})
        ok, reason = m_max.check_compatibility("1.0", "1.0.0")
        assert ok is False
        assert reason is not None and "maximum platform version" in reason


# ---------------------------------------------------------------------------
# 2. 插件发现与零原生依赖隔离测试
# ---------------------------------------------------------------------------


class TestPluginDiscoveryAndIsolation:
    """验证插件发现扫描在当前进程内零原生依赖导入。"""

    def test_discovery_zero_native_geospatial_imports(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        plugins_dir.mkdir(parents=True)

        # 模拟放置两个插件目录清单
        p1_dir = plugins_dir / "plug_alpha"
        p1_dir.mkdir()
        p1_manifest = PluginManifest(
            id="plug_alpha",
            name="Alpha Plugin",
            version="0.5.0",
            capabilities=[
                PluginCapabilitySpec(
                    capability_id="alpha:tool",
                    name="Tool",
                    entrypoint="dummy:tool",
                )
            ],
        )
        (p1_dir / "plugin.json").write_text(p1_manifest.model_dump_json(), encoding="utf-8")

        # 发现前断言：sys.modules 中无 osgeo / rasterio
        assert "osgeo" not in sys.modules, "发现前不应加载 osgeo"
        assert "rasterio" not in sys.modules, "发现前不应加载 rasterio"

        registry = CapabilityRegistry()
        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file, registry=registry)
        discovered = manager.discover()

        assert len(discovered) == 1
        assert discovered[0].manifest.id == "plug_alpha"
        assert discovered[0].status == PluginStatus.enabled

        # 发现后断言：sys.modules 中依旧严格零原生导入
        assert "osgeo" not in sys.modules, "插件扫描后不得加载 osgeo 模块"
        assert "rasterio" not in sys.modules, "插件扫描后不得加载 rasterio 模块"
        assert registry.get("alpha:tool") is not None


# ---------------------------------------------------------------------------
# 3. 插件安装、启用/禁用与卸载测试
# ---------------------------------------------------------------------------


class TestPluginInstallLifecycle:
    """验证插件从目录与 .zip 安装、启用/禁用状态切换与卸载。"""

    def test_install_from_folder_and_lifecycle(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        registry = CapabilityRegistry()
        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file, registry=registry)

        # 1. 从目录安装示范插件
        info = manager.install_plugin(SAMPLE_PLUGIN_SRC)
        assert info.manifest.id == "spectra_plugin_sample_stats"
        assert info.status == PluginStatus.enabled
        assert (plugins_dir / "spectra_plugin_sample_stats" / "plugin.json").exists()

        # 验证能力自动登记到 CapabilityRegistry
        cap_desc = registry.get("sample_stats:zscore")
        assert cap_desc is not None
        assert cap_desc.name == "Raster Z-Score Normalization"

        # 2. 禁用插件 -> 能力注销
        manager.disable_plugin("spectra_plugin_sample_stats")
        info = manager.get_plugin("spectra_plugin_sample_stats")
        assert info is not None
        assert info.status == PluginStatus.disabled
        assert registry.get("sample_stats:zscore") is None

        # 3. 重新启用插件 -> 能力重新注册
        manager.enable_plugin("spectra_plugin_sample_stats")
        info = manager.get_plugin("spectra_plugin_sample_stats")
        assert info is not None
        assert info.status == PluginStatus.enabled
        assert registry.get("sample_stats:zscore") is not None

        # 4. 卸载插件 -> 目录被移除、能力注销
        uninstalled = manager.uninstall_plugin("spectra_plugin_sample_stats")
        assert uninstalled is True
        assert not (plugins_dir / "spectra_plugin_sample_stats").exists()
        assert registry.get("sample_stats:zscore") is None
        assert manager.get_plugin("spectra_plugin_sample_stats") is None

    def test_install_from_zip_package(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        registry = CapabilityRegistry()
        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file, registry=registry)

        # 创建一个测试 zip 插件包
        zip_path = tmp_path / "sample_plugin.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in SAMPLE_PLUGIN_SRC.glob("*"):
                if item.is_file():
                    # 放入包内子目录
                    zf.write(item, arcname=f"sample_pkg/{item.name}")

        info = manager.install_plugin(zip_path)
        assert info.manifest.id == "spectra_plugin_sample_stats"
        assert info.status == PluginStatus.enabled
        assert (plugins_dir / "spectra_plugin_sample_stats" / "plugin.json").exists()
        assert registry.get("sample_stats:zscore") is not None


# ---------------------------------------------------------------------------
# 4. 不兼容检测与熔断隔离恢复测试
# ---------------------------------------------------------------------------


class TestPluginCompatibilityAndQuarantine:
    """验证不兼容提示与崩溃连续失败自动熔断隔离及恢复。"""

    def test_incompatible_plugin_quarantined_from_enablement(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        registry = CapabilityRegistry()

        # 构造 API 版本不兼容插件 (要求 API 99.0)
        incompat_dir = tmp_path / "incompat_src"
        incompat_dir.mkdir()
        m = PluginManifest(
            id="incompat_plugin",
            name="Incompatible Plugin",
            version="1.0.0",
            provider_api_version="99.0",
            capabilities=[
                PluginCapabilitySpec(
                    capability_id="incompat:calc",
                    name="Calc",
                    entrypoint="dummy:calc",
                )
            ],
        )
        (incompat_dir / "plugin.json").write_text(m.model_dump_json(), encoding="utf-8")

        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file, registry=registry)
        info = manager.install_plugin(incompat_dir)

        # 状态应为 incompatible，不能被注册，不能被直接启用
        assert info.status == PluginStatus.incompatible
        assert info.incompatibility_reason is not None
        assert "provider_api_version 99.0" in info.incompatibility_reason
        assert registry.get("incompat:calc") is None

        with pytest.raises(PluginError, match="版本不兼容"):
            manager.enable_plugin("incompat_plugin")

    def test_consecutive_crash_auto_quarantine_and_reset(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        registry = CapabilityRegistry()
        manager = PluginManager(
            plugins_dir=plugins_dir,
            state_file=state_file,
            registry=registry,
            failure_threshold=3,
        )

        info = manager.install_plugin(SAMPLE_PLUGIN_SRC)
        assert info.status == PluginStatus.enabled
        assert registry.get("sample_stats:zscore") is not None

        # 第 1 次失败
        manager.record_failure("spectra_plugin_sample_stats", "Error 1")
        assert manager.get_plugin("spectra_plugin_sample_stats").consecutive_failures == 1
        assert manager.get_plugin("spectra_plugin_sample_stats").status == PluginStatus.enabled

        # 第 2 次失败
        manager.record_failure("spectra_plugin_sample_stats", "Error 2")
        assert manager.get_plugin("spectra_plugin_sample_stats").consecutive_failures == 2
        assert manager.get_plugin("spectra_plugin_sample_stats").status == PluginStatus.enabled

        # 成功一次会清零
        manager.record_success("spectra_plugin_sample_stats")
        assert manager.get_plugin("spectra_plugin_sample_stats").consecutive_failures == 0

        # 连续 3 次失败达到阈值，触发自动熔断隔离
        manager.record_failure("spectra_plugin_sample_stats", "Fatal 1")
        manager.record_failure("spectra_plugin_sample_stats", "Fatal 2")
        manager.record_failure("spectra_plugin_sample_stats", "Fatal 3")

        info_quarantined = manager.get_plugin("spectra_plugin_sample_stats")
        assert info_quarantined.status == PluginStatus.failed
        assert info_quarantined.consecutive_failures == 3
        # 熔断后能力必须立即从注册表注销
        assert registry.get("sample_stats:zscore") is None

        # 尝试直接 enable 必须被拒绝
        with pytest.raises(PluginError, match="失败隔离状态"):
            manager.enable_plugin("spectra_plugin_sample_stats")

        # 调用 reset_plugin_failure 重置并恢复
        manager.reset_plugin_failure("spectra_plugin_sample_stats")
        info_recovered = manager.get_plugin("spectra_plugin_sample_stats")
        assert info_recovered.status == PluginStatus.enabled
        assert info_recovered.consecutive_failures == 0
        assert registry.get("sample_stats:zscore") is not None


# ---------------------------------------------------------------------------
# 5. Worker 子进程端到端执行示范插件（Z-Score 标准化）
# ---------------------------------------------------------------------------


class TestPluginWorkerSubprocessExecution:
    """验证示范插件在独立 Worker 子进程中的端到端执行与故障隔离。"""

    def test_sample_stats_zscore_execution(
        self, gdal_rt: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试 Worker 进程端到端执行 sample_stats:zscore 算法。"""
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir(parents=True)

        monkeypatch.setenv("SPECTRA_PLUGINS_DIR", str(plugins_dir))
        monkeypatch.setenv("SPECTRA_PLUGINS_STATE_FILE", str(state_file))

        # 1. 主进程安装插件
        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file)
        manager.install_plugin(SAMPLE_PLUGIN_SRC)

        # 2. 准备合成输入影像（64x64，像元值从 10 到 100 递增）
        input_tif = tmp_path / "input_raster.tif"
        _make_test_geotiff(gdal_rt, input_tif, width=64, height=64)

        target_tif = workspace_dir / "zscore_out.tif"

        # 3. 构造 TaskSpec
        spec = TaskSpec(
            task_id="task-p8-plugin-zscore",
            attempt=1,
            capability_id="sample_stats:zscore",
            descriptor_revision="1.0.0",
            engine=EngineConstraint(
                provider_id="plugin",
                runtime_id="plugin-worker",
                expected_version="1.0.0",
            ),
            project_root=str(ROOT),
            workspace=str(workspace_dir),
            inputs={
                "input": [
                    DatasetRef(
                        dataset_id="input_raster",
                        kind=DatasetKind.raster,
                        uri=input_tif.as_uri(),
                    )
                ]
            },
            parameters={"band": 1, "fail_mode": False},
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=target_tif.as_uri(),
                    overwrite=True,
                )
            ],
        )

        progress_events: list[dict[str, Any]] = []
        log_events: list[dict[str, Any]] = []

        def on_prog(env: Any) -> None:
            progress_events.append(env.payload)

        def on_lg(env: Any) -> None:
            log_events.append(env.payload)

        # 4. 启动独立 Worker 进程并执行任务
        custom_env = {
            "SPECTRA_PLUGINS_DIR": str(plugins_dir),
            "SPECTRA_PLUGINS_STATE_FILE": str(state_file),
        }

        worker = WorkerProcess(
            provider_id="plugin",
            worker_id="test-plugin-w1",
            custom_env=custom_env,
        )
        try:
            ready_env = worker.start(timeout=10.0)
            assert ready_env.payload["runtime_id"] == "plugin-worker"

            res = worker.execute_task(
                spec,
                on_progress=on_prog,
                on_log=on_lg,
                timeout=20.0,
            )

            # 验证终态结果
            assert res.status == TaskStatus.succeeded, f"任务执行失败: {res.error}"
            assert len(res.outputs) == 1
            out_info = res.outputs[0]
            assert out_info["key"] == "output"
            assert target_tif.exists(), f"成果文件未生成: {target_tif}"

            # 验证标准化后的统计值：mean 应接近 0，std 应接近 1
            mean_v, std_v = _read_raster_stats(gdal_rt, target_tif)
            assert abs(mean_v) < 1e-4, f"标准化均值应接近 0，实测: {mean_v}"
            assert abs(std_v - 1.0) < 1e-4, f"标准化标准差应接近 1，实测: {std_v}"

            # 验证进度和日志分帧接收
            assert any(p.get("phase") == "finished" for p in progress_events)
            assert any("统计结果" in log_item.get("message", "") for log_item in log_events)

        finally:
            worker.stop()

    def test_sample_stats_failure_mode_isolated(
        self, gdal_rt: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试插件在 fail_mode 下抛出异常被隔离为失败 TaskResult，Worker 进程不崩溃。"""
        plugins_dir = tmp_path / "plugins"
        state_file = tmp_path / "plugins_state.json"
        workspace_dir = tmp_path / "workspace_fail"
        workspace_dir.mkdir(parents=True)

        monkeypatch.setenv("SPECTRA_PLUGINS_DIR", str(plugins_dir))
        monkeypatch.setenv("SPECTRA_PLUGINS_STATE_FILE", str(state_file))

        manager = PluginManager(plugins_dir=plugins_dir, state_file=state_file)
        manager.install_plugin(SAMPLE_PLUGIN_SRC)

        input_tif = tmp_path / "input_raster_fail.tif"
        _make_test_geotiff(gdal_rt, input_tif, width=32, height=32)

        spec = TaskSpec(
            task_id="task-p8-plugin-fail",
            attempt=1,
            capability_id="sample_stats:zscore",
            descriptor_revision="1.0.0",
            engine=EngineConstraint(
                provider_id="plugin",
                runtime_id="plugin-worker",
                expected_version="1.0.0",
            ),
            project_root=str(ROOT),
            workspace=str(workspace_dir),
            inputs={
                "input": [
                    DatasetRef(
                        dataset_id="input_raster",
                        kind=DatasetKind.raster,
                        uri=input_tif.as_uri(),
                    )
                ]
            },
            parameters={"fail_mode": True},  # 注入异常
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=(workspace_dir / "fail_out.tif").as_uri(),
                )
            ],
        )

        custom_env = {
            "SPECTRA_PLUGINS_DIR": str(plugins_dir),
            "SPECTRA_PLUGINS_STATE_FILE": str(state_file),
        }

        worker = WorkerProcess(
            provider_id="plugin",
            worker_id="test-plugin-w-fail",
            custom_env=custom_env,
        )
        try:
            worker.start(timeout=10.0)
            res = worker.execute_task(spec, timeout=10.0)

            # 异常应被安全捕获转化为 TaskStatus.failed
            assert res.status == TaskStatus.failed
            assert res.error is not None
            assert "Simulated plugin intentional failure" in res.error["message"]

            # Worker 进程依旧处于就绪状态，可继续心跳
            hb = worker.heartbeat(timeout=3.0)
            assert hb is not None
        finally:
            worker.stop()
