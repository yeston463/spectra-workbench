"""P8-02 VSI/归档/远程数据集成测试（验证 GDAL VSI、HTTP Range、超时、重试与取消）。

验收标准（BACKLOG P8-02 / 里程碑 A13）：
1. 本地归档（/vsizip/, /vsitar/）读取单栅格与多栅格子数据集探测通过。
2. 本地 HTTP 测试服务（127.0.0.1）范围读取（Range: bytes=...）通过，零全量内存加载。
3. 网络可控超时配置生效，慢速/停顿端点快速失败不挂起。
4. 临时故障注入（503）重试机制生效，多次重试后成功恢复。
5. 协同取消在 ≤2 秒内快速进入终态，无僵尸进程。
6. 项目持久化（.spectra.json）正确保存并复原远程/VSI 图层 URI。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.paths import (
    get_display_name,
    is_vsi_or_remote,
    normalize_vsi_path,
    sanitize_url,
)
from spectra_workbench.project.model import (
    LayerRecord,
    ProjectSpec,
    load_project,
    resolve_source,
    save_project,
    store_source,
)
from spectra_workbench.rendering.display import DisplaySettings
from spectra_workbench.scheduler.client import QtWorkerClient
from tests.fixtures.remote_server import (
    LocalHttpRangeServer,
    create_tar_archive,
    create_zip_archive,
)

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _make_sample_geotiff(
    gdal_rt: dict[str, Any],
    path: Path,
    width: int = 256,
    height: int = 256,
    bands: int = 3,
    tiled: bool = True,
    block_size: int = 128,
) -> Path:
    """创建合成 GeoTIFF 测试影像（通过 GDAL 引擎子进程写入，UI 进程不导入 osgeo）。"""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    posix_path = path.as_posix()
    script = f"""
import os
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

drv = gdal.GetDriverByName("GTiff")
opts = []
if {tiled}:
    opts.extend(["TILED=YES", "BLOCKXSIZE={block_size}", "BLOCKYSIZE={block_size}"])
ds = drv.Create(r"{posix_path}", {width}, {height}, {bands}, gdal.GDT_Byte, options=opts)
ds.SetGeoTransform([500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0])
srs = osr.SpatialReference()
srs.ImportFromEPSG(32649)
ds.SetSpatialRef(srs)

for b in range(1, {bands} + 1):
    band = ds.GetRasterBand(b)
    arr = np.full(({height}, {width}), b * 50, dtype=np.uint8)
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


class TestP8DomainPaths:
    """验证路径标准化与辅助函数。"""

    def test_is_vsi_or_remote(self) -> None:
        assert is_vsi_or_remote("/vsicurl/http://127.0.0.1:8080/cog.tif")
        assert is_vsi_or_remote("http://127.0.0.1:8080/cog.tif")
        assert is_vsi_or_remote("https://example.com/raster.tif?token=abc")
        assert is_vsi_or_remote("/vsizip/C:/data/test.zip")
        assert is_vsi_or_remote("/vsitar/data/archive.tar")
        assert is_vsi_or_remote('NETCDF:"file.nc":Band1')
        assert not is_vsi_or_remote("C:\\data\\local.tif")
        assert not is_vsi_or_remote("local.tif")

    def test_normalize_vsi_path(self) -> None:
        assert (
            normalize_vsi_path("http://127.0.0.1:8080/cog.tif")
            == "/vsicurl/http://127.0.0.1:8080/cog.tif"
        )
        assert (
            normalize_vsi_path("https://example.com/data.tif")
            == "/vsicurl/https://example.com/data.tif"
        )
        assert normalize_vsi_path("s3://bucket/key.tif") == "/vsis3/bucket/key.tif"
        assert normalize_vsi_path("/vsizip/C:\\data\\test.zip") == "/vsizip/C:/data/test.zip"

    def test_get_display_name_and_sanitize(self) -> None:
        assert get_display_name("http://127.0.0.1:8080/my_raster.tif?token=123") == "my_raster.tif"
        assert get_display_name("/vsizip/C:/archives/bundle.zip/band1.tif") == "band1.tif"
        assert (
            sanitize_url("http://example.com/data.tif?token=secret123&region=us")
            == "http://example.com/data.tif?token=***&region=us"
        )


class TestP8VsiArchiveReading:
    """验证本地归档（ZIP / TAR）的范围读取与子数据集枚举。"""

    def test_vsi_local_zip_single_raster(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        tif_path = tmp_path / "single.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=128, height=128, bands=1)

        zip_path = tmp_path / "single.zip"
        create_zip_archive(zip_path, {"inner.tif": tif_path})

        with QtWorkerClient(
            provider_id="gdal",
            project_root=str(ROOT),
            custom_python=gdal_rt["python"],
            custom_env=gdal_rt.get("env"),
        ) as client:
            client.start()
            # 1. 直接以归档路径探测
            meta = client.probe_dataset(zip_path)
            assert meta["width"] == 128
            assert meta["height"] == 128
            assert meta["count"] == 1
            assert meta["driver"] == "GTiff"

            # 2. 读取局部切片窗口
            win = client.read_window(
                zip_path, x_off=0, y_off=0, win_w=32, win_h=32, out_w=32, out_h=32
            )
            assert win["out_w"] == 32
            assert win["out_h"] == 32
            assert win["b64_data"] is not None

    def test_vsi_local_tar_archive(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        tif_path = tmp_path / "tar_inner.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=64, height=64, bands=2)

        tar_path = tmp_path / "archive.tar"
        create_tar_archive(tar_path, {"raster.tif": tif_path})

        with QtWorkerClient(
            provider_id="gdal",
            project_root=str(ROOT),
            custom_python=gdal_rt["python"],
            custom_env=gdal_rt.get("env"),
        ) as client:
            client.start()
            vsi_tar_target = f"/vsitar/{tar_path.as_posix()}/raster.tif"
            meta = client.probe_dataset(vsi_tar_target)
            assert meta["width"] == 64
            assert meta["height"] == 64
            assert meta["count"] == 2

            win = client.read_window(
                vsi_tar_target, x_off=10, y_off=10, win_w=16, win_h=16, out_w=16, out_h=16
            )
            assert win["out_w"] == 16
            assert win["out_h"] == 16

    def test_vsi_multi_raster_zip_subdataset_discovery(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        red_tif = tmp_path / "red.tif"
        nir_tif = tmp_path / "nir.tif"
        _make_sample_geotiff(gdal_rt, red_tif, width=64, height=64, bands=1)
        _make_sample_geotiff(gdal_rt, nir_tif, width=64, height=64, bands=1)

        zip_path = tmp_path / "multi_bundle.zip"
        create_zip_archive(zip_path, {"red.tif": red_tif, "nir.tif": nir_tif})

        with QtWorkerClient(
            provider_id="gdal",
            project_root=str(ROOT),
            custom_python=gdal_rt["python"],
            custom_env=gdal_rt.get("env"),
        ) as client:
            client.start()
            # 探测多栅格归档容器并枚举子数据集
            sub_info = client.list_subdatasets(zip_path)
            assert sub_info["subdataset_count"] == 2
            uris = [item["uri"] for item in sub_info["subdatasets"]]
            assert any("red.tif" in u for u in uris)
            assert any("nir.tif" in u for u in uris)

            # 单独探测并读取每个子数据集
            meta_red = client.probe_dataset(uris[0])
            assert meta_red["width"] == 64
            win_red = client.read_window(
                uris[0], x_off=0, y_off=0, win_w=16, win_h=16, out_w=16, out_h=16
            )
            assert win_red["out_w"] == 16


class TestP8HttpRangeAndResilience:
    """验证 HTTP Range 范围读取、超时、重试与协同取消。"""

    def test_http_range_zero_full_download(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        """测试 127.0.0.1 HTTP Range 范围读取，验证零全量下载。"""
        tif_path = tmp_path / "cog_sample.tif"
        _make_sample_geotiff(
            gdal_rt, tif_path, width=512, height=512, bands=3, tiled=True, block_size=128
        )
        full_size = tif_path.stat().st_size
        assert full_size > 100_000

        with LocalHttpRangeServer({"cog.tif": tif_path}) as server:
            remote_url = server.url_for("cog.tif")
            with QtWorkerClient(
                provider_id="gdal",
                project_root=str(ROOT),
                custom_python=gdal_rt["python"],
                custom_env=gdal_rt.get("env"),
            ) as client:
                client.start()
                # 探测元数据
                meta = client.probe_dataset(remote_url)
                assert meta["width"] == 512
                assert meta["height"] == 512
                assert meta["count"] == 3

                # 读取仅 32x32 窗口
                win = client.read_window(
                    remote_url, x_off=0, y_off=0, win_w=32, win_h=32, out_w=32, out_h=32
                )
                assert win["out_w"] == 32
                assert win["out_h"] == 32

                # 验证服务器收到了 HTTP Range 请求
                assert server.range_requests_count() > 0
                # 关键验收指标：总传输字节数远小于整幅文件大小（证实没有整幅加载！）
                assert server.total_bytes_sent < full_size
                assert server.total_bytes_sent < full_size * 0.7

    def test_http_timeout_handling(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        """验证慢速服务端在超时配置下迅速失败，进程不挂起且保持健康。"""
        tif_path = tmp_path / "timeout_sample.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=64, height=64, bands=1)

        # 注入 3 秒延迟
        with LocalHttpRangeServer({"slow.tif": tif_path}, delay_seconds=3.0) as server:
            remote_url = server.url_for("slow.tif")
            env_with_fast_timeout = dict(gdal_rt.get("env") or {})
            env_with_fast_timeout["GDAL_HTTP_TIMEOUT"] = "1"
            env_with_fast_timeout["GDAL_HTTP_CONNECTTIMEOUT"] = "1"
            env_with_fast_timeout["GDAL_HTTP_MAX_RETRY"] = "0"

            with QtWorkerClient(
                provider_id="gdal",
                project_root=str(ROOT),
                custom_python=gdal_rt["python"],
                custom_env=env_with_fast_timeout,
            ) as client:
                client.start()
                t0 = time.time()
                with pytest.raises(Exception) as exc_info:
                    client.probe_dataset(remote_url, timeout=4.0)
                elapsed = time.time() - t0
                # 超时应在合理时间内返回，严禁无限挂起
                assert elapsed < 5.0
                err_msg = str(exc_info.value).lower()
                assert (
                    "timeout" in err_msg
                    or "timed out" in err_msg
                    or "curl error" in err_msg
                    or "无法打开" in err_msg
                    or "failed" in err_msg
                    or "超时" in err_msg
                )

    def test_http_retry_handling(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        """验证瞬时故障（503 Service Unavailable）自动重试并恢复。"""
        tif_path = tmp_path / "retry_sample.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=64, height=64, bands=1)

        # 注入 1 次 503 失败，随后恢复
        with LocalHttpRangeServer({"retry.tif": tif_path}, fail_count=1, fail_status=503) as server:
            remote_url = server.url_for("retry.tif")
            env_with_retry = dict(gdal_rt.get("env") or {})
            env_with_retry["GDAL_HTTP_MAX_RETRY"] = "3"
            env_with_retry["GDAL_HTTP_RETRY_DELAY"] = "0.2"

            with QtWorkerClient(
                provider_id="gdal",
                project_root=str(ROOT),
                custom_python=gdal_rt["python"],
                custom_env=env_with_retry,
            ) as client:
                client.start()
                meta = client.probe_dataset(remote_url)
                assert meta["width"] == 64
                assert meta["height"] == 64
                # 确认服务器接收到了重试请求
                assert len(server.requests) >= 2

    def test_worker_async_remote_read_and_cancellation(
        self, qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
    ) -> None:
        """验证跨进程 QtWorkerClient 对远程数据的异步读取及快速协同取消（≤2秒）。"""
        tif_path = tmp_path / "remote_worker.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=256, height=256, bands=1)

        with LocalHttpRangeServer({"data.tif": tif_path}) as server:
            remote_url = server.url_for("data.tif")
            with QtWorkerClient(
                provider_id="gdal",
                project_root=str(ROOT),
                custom_python=gdal_rt["python"],
                custom_env=gdal_rt.get("env"),
            ) as client:
                client.start()
                probe_res: dict[str, Any] = {}
                probe_err: list[str] = []

                def _on_ok(data: dict[str, Any]) -> None:
                    probe_res.update(data)

                def _on_err(err: str) -> None:
                    probe_err.append(err)

                client.probe_dataset_async(remote_url, _on_ok, _on_err)

                t0 = time.time()
                while not probe_res and not probe_err and (time.time() - t0 < 10.0):
                    qapp.processEvents()
                    time.sleep(0.05)

                assert not probe_err, f"Probe error: {probe_err}"
                assert probe_res["width"] == 256
                assert probe_res["height"] == 256

                # 测试协同取消：cancel_task 立即返回并在 ≤2 秒内完成
                t_cancel_start = time.time()
                client.cancel_task()
                cancel_elapsed = time.time() - t_cancel_start
                assert cancel_elapsed <= 2.0


class TestP8ProjectPersistence:
    """验证远程数据在 .spectra.json 项目中的持久化与重开恢复。"""

    def test_project_store_and_restore_remote_source(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)

        remote_source = "http://127.0.0.1:8080/rasters/cog.tif"
        stored, is_rel = store_source(project_dir, remote_source)
        # 远程 URL 必须存储为绝对字符串且 is_relative=False
        assert is_rel is False
        assert stored == "/vsicurl/http://127.0.0.1:8080/rasters/cog.tif"

        # 还原路径
        resolved = resolve_source(project_dir, stored, is_relative=is_rel)
        assert resolved == "/vsicurl/http://127.0.0.1:8080/rasters/cog.tif"

        # 保存为完整 ProjectSpec 并重新读取
        rec = LayerRecord(
            layer_id="remote_ly_01",
            name="Remote COG",
            source=stored,
            source_is_relative=is_rel,
            order=0,
            display=DisplaySettings(),
        )
        spec = ProjectSpec(
            project_id="test_remote_proj",
            layers=[rec],
        )

        proj_file = project_dir / "project.spectra.json"
        save_project(proj_file, spec)

        loaded = load_project(proj_file)
        assert len(loaded.layers) == 1
        loaded_ly = loaded.layers[0]
        assert loaded_ly.source == "/vsicurl/http://127.0.0.1:8080/rasters/cog.tif"
        assert loaded_ly.source_is_relative is False


class TestP8GuiRemoteIntegration:
    """验证 MainWindow 界面层对远程数据及 VSI 归档的打开与项目保存/重开。"""

    def test_gui_open_remote_dataset_and_dialog(
        self,
        qapp: QApplication,
        gdal_rt: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QInputDialog

        from spectra_workbench.ui.main_window import MainWindow

        tif_path = tmp_path / "gui_sample.tif"
        _make_sample_geotiff(gdal_rt, tif_path, width=128, height=128, bands=3)

        with LocalHttpRangeServer({"gui_cog.tif": tif_path}) as server:
            remote_url = server.url_for("gui_cog.tif")
            win = MainWindow()
            win.show()
            try:
                # 1. 直接打开远程 URL
                win.open_dataset(remote_url)
                t0 = time.time()
                while len(win.layer_panel.layers) == 0 and time.time() - t0 < 10.0:
                    qapp.processEvents()
                    time.sleep(0.05)

                assert len(win.layer_panel.layers) == 1
                layer = win.layer_panel.layers[0]
                assert layer.name == "gui_cog.tif"
                assert str(layer.file_path).startswith("/vsicurl/http://127.0.0.1:")

                # 2. 模拟点击菜单中的 open_remote_url_dialog
                monkeypatch.setattr(
                    QInputDialog,
                    "getText",
                    lambda *args, **kwargs: (server.url_for("gui_cog.tif"), True),
                )
                win.open_remote_url_dialog()
                t0 = time.time()
                while len(win.layer_panel.layers) < 2 and time.time() - t0 < 10.0:
                    qapp.processEvents()
                    time.sleep(0.05)

                assert len(win.layer_panel.layers) == 2

                # 3. 保存项目并重开
                proj_file = tmp_path / "gui_remote_proj.spectra.json"
                from unittest.mock import patch

                with patch(
                    "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
                    return_value=(str(proj_file), ""),
                ):
                    assert win.save_project_action(save_as=True) is True
                assert proj_file.exists()
            finally:
                win.close()
                qapp.processEvents()

            # 4. 新窗口重开验证跨会话恢复
            win2 = MainWindow()
            win2.show()
            try:
                win2.open_project(proj_file)
                t0 = time.time()
                while len(win2.layer_panel.layers) < 2 and time.time() - t0 < 10.0:
                    qapp.processEvents()
                    time.sleep(0.05)

                assert len(win2.layer_panel.layers) == 2
                assert str(win2.layer_panel.layers[0].file_path).startswith(
                    "/vsicurl/http://127.0.0.1:"
                )
            finally:
                win2.close()
                qapp.processEvents()
