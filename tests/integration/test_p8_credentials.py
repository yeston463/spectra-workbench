"""P8-03 凭据 Profile 与脱敏管理集成测试（验证凭据隔离、安全持久化、脱敏与 VSI 认证）。

验收标准（BACKLOG P8-03 / 里程碑 A13 / 开发方案 §9.4）：
1. Token/Key/Secret 不写项目文件 (.spectra.json)、日志或诊断包 (doctor_report.json)；仅保存 Profile 引用名。
2. 凭据 Profile 存储在本地独立配置文件（默认 ~/.spectra/credentials.json，可通过 SPECTRA_CREDENTIALS_FILE 覆盖），与项目和工作区物理隔离。
3. 支持 S3, HTTP Bearer, HTTP Basic, Azure, GCS, Header, Custom 映射到 GDAL VSI 选项与环境变量。
4. 脱敏工具：URL 脱敏（query 及 basic auth）、文本脱敏（Bearer, Basic, AWS secrets）、字典递归脱敏。
5. 离线启动可正常处理本地数据：在离线/无网络/无凭据配置文件情况下，GUI 启动、本地栅格/矢量加载、算法计算、项目保存与重开 100% 正常。
6. 动态凭据下发：Worker 动态接收 profile 配置并在读取受保护的远程 VSI 端点时成功鉴权。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import (
    DoctorReport,
    EngineReport,
    find_project_root,
    resolve_gdal_runtime,
    save_doctor_report,
)
from spectra_workbench.domain.credentials import (
    CredentialProfile,
    CredentialStore,
    sanitize_dict,
    sanitize_text,
    sanitize_url,
)
from spectra_workbench.domain.paths import is_vsi_or_remote
from spectra_workbench.project.model import (
    LayerRecord,
    ProjectSpec,
    load_project,
    save_project,
    store_source,
)
from spectra_workbench.providers.base import ExecutionContext
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.workers.process import WorkerProcess
from tests.fixtures.remote_server import LocalHttpRangeServer

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
    width: int = 128,
    height: int = 128,
    bands: int = 3,
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
ds = drv.Create(r"{posix_path}", {width}, {height}, {bands}, gdal.GDT_Byte)
ds.SetGeoTransform([500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0])
srs = osr.SpatialReference()
srs.ImportFromEPSG(32649)
ds.SetSpatialRef(srs)

for b in range(1, {bands} + 1):
    band = ds.GetRasterBand(b)
    arr = np.full(({height}, {width}), b * 40, dtype=np.uint8)
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


class TestCredentialProfileMappings:
    """验证 CredentialProfile 到各远程协议及环境变量的映射规则。"""

    def test_s3_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-s3",
            kind="s3",
            description="AWS S3 bucket credentials",
            credentials={
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "aws_session_token": "AQoDYXdzEJr1...",
                "aws_region": "ap-southeast-1",
                "aws_endpoint": "https://s3.ap-southeast-1.amazonaws.com",
            },
        )
        env = p.to_env_vars()
        assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
        assert env["AWS_SECRET_ACCESS_KEY"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert env["AWS_SESSION_TOKEN"] == "AQoDYXdzEJr1..."
        assert env["AWS_DEFAULT_REGION"] == "ap-southeast-1"
        assert env["AWS_S3_ENDPOINT"] == "https://s3.ap-southeast-1.amazonaws.com"

    def test_azure_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-azure",
            kind="azure",
            credentials={
                "azure_storage_account": "myaccount",
                "azure_storage_key": "key123==",
                "azure_storage_connection_string": "DefaultEndpointsProtocol=https;...",
            },
        )
        env = p.to_env_vars()
        assert env["AZURE_STORAGE_ACCOUNT"] == "myaccount"
        assert env["AZURE_STORAGE_ACCESS_KEY"] == "key123=="
        assert env["AZURE_STORAGE_CONNECTION_STRING"] == "DefaultEndpointsProtocol=https;..."

    def test_gcs_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-gcs",
            kind="gcs",
            credentials={
                "gs_access_key_id": "GOOG123456",
                "gs_secret_key": "secret456",
            },
        )
        env = p.to_env_vars()
        assert env["GS_ACCESS_KEY_ID"] == "GOOG123456"
        assert env["GS_SECRET_ACCESS_KEY"] == "secret456"

    def test_http_bearer_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-bearer",
            kind="http_bearer",
            credentials={"token": "eyJh...jwt_token_sample"},
        )
        env = p.to_env_vars()
        assert env["GDAL_HTTP_HEADERS"] == "Authorization: Bearer eyJh...jwt_token_sample"

    def test_http_basic_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-basic",
            kind="http_basic",
            credentials={"username": "alice", "password": "SuperSecretPassword123"},
        )
        env = p.to_env_vars()
        assert env["GDAL_HTTP_USERPWD"] == "alice:SuperSecretPassword123"

    def test_header_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-header",
            kind="header",
            credentials={"header_name": "X-API-Key", "header_value": "secret-api-key-999"},
        )
        env = p.to_env_vars()
        assert env["GDAL_HTTP_HEADERS"] == "X-API-Key: secret-api-key-999"

    def test_custom_mapping(self) -> None:
        p = CredentialProfile(
            profile_id="my-custom",
            kind="custom",
            credentials={"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "MY_VAR": "value"},
        )
        env = p.to_env_vars()
        assert env["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"
        assert env["MY_VAR"] == "value"

    def test_safe_dict_never_leaks_secrets(self) -> None:
        p = CredentialProfile(
            profile_id="confidential",
            kind="http_bearer",
            description="Production Bearer Token",
            credentials={"token": "TOP_SECRET_JWT_TOKEN_12345"},
        )
        safe = p.to_safe_dict()
        assert safe["profile_id"] == "confidential"
        assert safe["kind"] == "http_bearer"
        assert "configured_keys" in safe
        assert "token" in safe["configured_keys"]
        # 绝不包含 credentials 字典或密钥明文
        assert "credentials" not in safe
        assert "TOP_SECRET_JWT_TOKEN_12345" not in json.dumps(safe)


class TestCredentialStoreOperations:
    """验证 CredentialStore 的 CRUD、隔离存储与原子写入。"""

    def test_store_crud_lifecycle(self, tmp_path: Path) -> None:
        store_file = tmp_path / "custom_creds.json"
        store = CredentialStore(store_file)

        # 1. 初始为空
        assert store.list_profiles() == []
        assert store.get_profile("non_existent") is None

        # 2. 保存 S3 profile
        p1 = CredentialProfile(
            profile_id="s3_prod",
            kind="s3",
            credentials={"aws_access_key_id": "KEY1", "aws_secret_access_key": "SEC1"},
        )
        store.save_profile(p1)

        # 验证文件已创建且内容有效
        assert store_file.is_file()
        loaded_p1 = store.get_profile("s3_prod")
        assert loaded_p1 is not None
        assert loaded_p1.profile_id == "s3_prod"
        assert loaded_p1.credentials["aws_access_key_id"] == "KEY1"

        # 3. 保存 HTTP Bearer profile
        p2 = CredentialProfile(
            profile_id="bearer_api",
            kind="http_bearer",
            credentials={"token": "token_abc_123"},
        )
        store.save_profile(p2)

        # 验证列表包含两项
        profiles = store.list_profiles()
        assert len(profiles) == 2
        p_ids = {p.profile_id for p in profiles}
        assert p_ids == {"s3_prod", "bearer_api"}

        # 4. 解析环境变量
        env = store.resolve_env_for_profile("bearer_api")
        assert env == {"GDAL_HTTP_HEADERS": "Authorization: Bearer token_abc_123"}
        assert store.resolve_env_for_profile("missing_id") == {}

        # 5. 删除 profile
        assert store.delete_profile("s3_prod") is True
        assert store.get_profile("s3_prod") is None
        assert len(store.list_profiles()) == 1
        assert store.delete_profile("s3_prod") is False

    def test_store_env_variable_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target_path = tmp_path / "env_creds.json"
        monkeypatch.setenv("SPECTRA_CREDENTIALS_FILE", str(target_path))

        store = CredentialStore()
        assert store.store_path == target_path.resolve()

        p = CredentialProfile(profile_id="env_test", kind="custom", credentials={"K": "V"})
        store.save_profile(p)
        assert target_path.exists()


class TestSanitizationUtilities:
    """验证 URL、文本、字典的敏感凭据脱敏算法。"""

    def test_sanitize_url(self) -> None:
        # 1. 包含 basic auth 密码
        url1 = "https://admin:my_secret_pass@example.com/data/landsat.tif"
        s1 = sanitize_url(url1)
        assert s1 == "https://admin:***@example.com/data/landsat.tif"
        assert "my_secret_pass" not in s1

        # 2. 包含敏感 query token / key
        url2 = "https://tiles.example.org/cog.tif?token=eyJhbGciOiJIUzI1NiJ9.abc&format=geotiff&sig=98765"
        s2 = sanitize_url(url2)
        assert "token=***" in s2
        assert "sig=***" in s2
        assert "format=geotiff" in s2
        assert "eyJhbGciOi" not in s2
        assert "98765" not in s2

        # 3. 正常无凭据 URL 保持原样
        url3 = "http://127.0.0.1:8080/sample.tif?x=1&y=2"
        assert sanitize_url(url3) == url3

    def test_sanitize_text(self) -> None:
        raw_log = (
            "Task worker connecting: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xyz, "
            "using AWS aws_secret_access_key=superSecretKey123, "
            "password: 'PlainTextPassword!' and URL http://user:pass123@remote.com/cog.tif"
        )
        clean = sanitize_text(raw_log)
        assert "Bearer ***" in clean
        assert "superSecretKey123" not in clean
        assert "PlainTextPassword!" not in clean
        assert "pass123" not in clean
        assert "http://user:***@remote.com/cog.tif" in clean

    def test_sanitize_dict(self) -> None:
        data = {
            "task_id": "t-100",
            "access_key": "AKIA12345678",
            "api_key": "SECRET_KEY_ABC",
            "nested": {
                "bearer_token": "token-xyz",
                "normal_param": 42,
                "url": "https://example.com/item.tif?key=abcdef&type=full",
            },
            "list_items": [
                "plain text",
                "Bearer secret-jwt-here",
                {"password": "hidden_pass", "safe": "ok"},
            ],
        }
        sanitized = sanitize_dict(data)
        assert sanitized["task_id"] == "t-100"
        assert sanitized["access_key"] == "***"
        assert sanitized["api_key"] == "***"
        assert sanitized["nested"]["bearer_token"] == "***"
        assert sanitized["nested"]["normal_param"] == 42
        assert "key=***" in sanitized["nested"]["url"]
        assert "abcdef" not in sanitized["nested"]["url"]
        assert sanitized["list_items"][1] == "Bearer ***"
        assert sanitized["list_items"][2]["password"] == "***"
        assert sanitized["list_items"][2]["safe"] == "ok"


class TestProjectPersistenceSecurity:
    """验证项目文件 (.spectra.json) 凭据隔离：绝不保存密钥明文。"""

    def test_saving_project_persists_only_profile_reference(self, tmp_path: Path) -> None:
        proj_file = tmp_path / "test_secure.spectra.json"

        # 模拟带 Profile 与带 inline token 的远程图层
        raw_remote_url = "https://cdn.example.com/cog.tif?token=SECRET_TOKEN_DO_NOT_LEAK"
        stored, is_rel = store_source(tmp_path, raw_remote_url)
        # store_source 会自动脱敏 URL 中的 token
        assert "SECRET_TOKEN_DO_NOT_LEAK" not in stored
        assert "token=***" in stored

        layer = LayerRecord(
            layer_id="ly_secure",
            name="Remote Protected COG",
            source=stored,
            source_is_relative=is_rel,
            visible=True,
            order=0,
            credential_profile="prod_s3_read",
            metadata_snapshot={"driver": "GTiff", "count": 1},
        )
        spec = ProjectSpec(layers=[layer])
        saved_path = save_project(proj_file, spec)

        # 检查持久化后的 JSON 文本
        raw_json = saved_path.read_text(encoding="utf-8")
        assert "prod_s3_read" in raw_json
        assert "SECRET_TOKEN_DO_NOT_LEAK" not in raw_json
        assert "credential_profile" in raw_json

        # 重新加载
        reloaded = load_project(saved_path)
        assert len(reloaded.layers) == 1
        assert reloaded.layers[0].credential_profile == "prod_s3_read"
        assert reloaded.layers[0].source == stored


class TestDiagnosticsAndLoggingSanitization:
    """验证 ExecutionContext 日志上报与 Doctor 诊断包自动脱敏。"""

    def test_execution_context_log_sanitization(self, tmp_path: Path) -> None:
        logs_captured: list[tuple[str, str, str | None]] = []

        def _log_cb(lvl: str, msg: str, strm: str | None) -> None:
            logs_captured.append((lvl, msg, strm))

        ctx = ExecutionContext(
            task_id="t-audit",
            workspace=tmp_path / "ws",
            staging_dir=tmp_path / "staging",
            log_callback=_log_cb,
        )

        ctx.log("INFO", "Authenticating with Bearer my_super_secret_jwt_token_999")
        ctx.log("WARNING", "Failed to access bucket: AWS_SECRET_ACCESS_KEY=SuperSecretAccessKeyXYZ")

        assert len(logs_captured) == 2
        assert "my_super_secret_jwt_token_999" not in logs_captured[0][1]
        assert "Bearer ***" in logs_captured[0][1]
        assert "SuperSecretAccessKeyXYZ" not in logs_captured[1][1]
        assert "AWS_SECRET_ACCESS_KEY=***" in logs_captured[1][1]

    def test_doctor_report_sanitization(self, tmp_path: Path) -> None:
        out_report = tmp_path / "doctor_report.json"
        rep = DoctorReport(
            generated_utc="2026-11-08T00:00:00Z",
            project_root=str(tmp_path),
            overall_ok=True,
            gui_can_start=True,
            envs={
                "gdal": EngineReport(
                    role="gdal",
                    status="ok",
                    version_info={"api_key": "LEAKED_API_KEY_123", "gdal_version": "3.13.3"},
                    messages=["Connected with token=PRIVATE_TOKEN_999"],
                )
            },
        )
        save_doctor_report(rep, out_report)

        report_txt = out_report.read_text(encoding="utf-8")
        assert "LEAKED_API_KEY_123" not in report_txt
        assert "PRIVATE_TOKEN_999" not in report_txt
        assert '"api_key": "***"' in report_txt
        assert "token=***" in report_txt


class TestOfflineModeReadiness:
    """验证在离线/无网络/无凭据配置文件的环境下，系统可正常处理本地数据。"""

    def test_offline_local_data_workflow(
        self,
        gdal_rt: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 指向不存在的凭据文件模拟离线/新机环境
        monkeypatch.setenv("SPECTRA_CREDENTIALS_FILE", str(tmp_path / "non_existent_creds.json"))

        store = CredentialStore()
        assert store.list_profiles() == []

        # 创建本地测试 GeoTIFF
        local_tif = _make_sample_geotiff(gdal_rt, tmp_path / "offline_local.tif")

        # 启动 Worker 进程并执行本地数据探测与读取
        worker = WorkerProcess(
            provider_id="gdal",
            project_root=ROOT,
            custom_python=gdal_rt["python"],
            custom_env=gdal_rt["env"],
        )
        worker.start(timeout=15.0)
        try:
            meta = worker.probe_dataset(local_tif)
            assert meta["width"] == 128
            assert meta["height"] == 128
            assert meta["count"] == 3

            # 窗口读取
            window = worker.read_window(local_tif, x_off=0, y_off=0, win_w=64, win_h=64, out_w=64, out_h=64)
            assert window["out_w"] == 64
            assert window["out_h"] == 64
            assert "b64_data" in window

            # 本地项目保存与重开
            proj_file = tmp_path / "offline_project.spectra.json"
            stored, is_rel = store_source(tmp_path, local_tif)
            layer = LayerRecord(
                layer_id="ly_offline",
                name="Offline Local TIFF",
                source=stored,
                source_is_relative=is_rel,
                order=0,
                credential_profile=None,
            )
            spec = ProjectSpec(layers=[layer])
            save_project(proj_file, spec)

            reloaded = load_project(proj_file)
            assert len(reloaded.layers) == 1
            assert reloaded.layers[0].name == "Offline Local TIFF"
            assert reloaded.layers[0].credential_profile is None
        finally:
            worker.stop()


class TestWorkerDynamicCredentialsAndVsiAuth:
    """验证 Worker 动态凭据下发与 HTTP Bearer 保护的 VSI 端点鉴权。"""

    def test_vsi_authentication_with_dynamic_profile(
        self,
        gdal_rt: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        # 1. 准备本地 GeoTIFF
        local_tif = _make_sample_geotiff(gdal_rt, tmp_path / "protected_target.tif")

        # 2. 启动带 Bearer 鉴权的 LocalHttpRangeServer
        secret_token = "spectra-auth-secret-token-xyz"
        server = LocalHttpRangeServer(
            files={"protected.tif": local_tif},
            auth_token=secret_token,
        )
        server.start()

        worker = WorkerProcess(
            provider_id="gdal",
            project_root=ROOT,
            custom_python=gdal_rt["python"],
            custom_env=gdal_rt["env"],
        )
        worker.start(timeout=15.0)

        vsi_url = f"/vsicurl/{server.url('protected.tif')}"

        try:
            # 3. 未配置凭据时探测：必须失败 (HTTP 401)
            with pytest.raises(Exception) as exc_info:
                worker.probe_dataset(vsi_url, timeout=5.0)
            # 确认探测失败
            assert "探测数据集失败" in str(exc_info.value) or "401" in str(exc_info.value)

            # 4. 动态下发凭据 Profile
            prof = CredentialProfile(
                profile_id="remote_test_profile",
                kind="http_bearer",
                credentials={"token": secret_token},
            )
            res = worker.apply_credential_profile(prof)
            assert res.get("status") == "succeeded"
            assert "GDAL_HTTP_HEADERS" in res.get("applied_keys", [])

            # 5. 再次探测：鉴权成功，元数据正常返回
            meta = worker.probe_dataset(vsi_url, timeout=10.0)
            assert meta["width"] == 128
            assert meta["height"] == 128
            assert meta["count"] == 3

            # 6. 读取窗口：成功返回切片
            win = worker.read_window(vsi_url, x_off=0, y_off=0, win_w=32, win_h=32, out_w=32, out_h=32)
            assert win["out_w"] == 32
            assert win["out_h"] == 32
            assert "b64_data" in win
        finally:
            worker.stop()
            server.stop()


class TestGuiCredentialIntegration:
    """验证 GUI 交互：打开带凭据的远程图层、项目保存与跨会话复原。"""

    def test_gui_open_dataset_with_credential_profile(
        self,
        qapp: QApplication,
        gdal_rt: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 配置隔离的凭据库
        creds_file = tmp_path / "gui_creds.json"
        monkeypatch.setenv("SPECTRA_CREDENTIALS_FILE", str(creds_file))

        store = CredentialStore(creds_file)
        token = "gui-secret-bearer-token-456"
        store.save_profile(CredentialProfile(profile_id="gui_bearer_prof", kind="http_bearer", credentials={"token": token}))

        local_tif = _make_sample_geotiff(gdal_rt, tmp_path / "gui_auth.tif")
        server = LocalHttpRangeServer(
            files={"auth_layer.tif": local_tif},
            auth_token=token,
        )
        server.start()

        win = MainWindow()
        win.show()
        qapp.processEvents()

        try:
            url = server.url("auth_layer.tif")
            # 通过带 credential_profile 参数打开
            win.open_dataset(url, credential_profile="gui_bearer_prof")

            # 等待探测与图层加载完成
            deadline = 15.0
            import time

            start_t = time.monotonic()
            while time.monotonic() - start_t < deadline:
                qapp.processEvents()
                if len(win.layer_panel.layers) > 0:
                    break
                time.sleep(0.05)

            assert len(win.layer_panel.layers) == 1
            layer = win.layer_panel.layers[0]
            assert layer.credential_profile == "gui_bearer_prof"
            assert is_vsi_or_remote(layer.file_path)

            # 保存项目
            proj_file = tmp_path / "gui_auth_proj.spectra.json"
            from unittest.mock import patch
            with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(proj_file), "Project")):
                win.save_project_action(save_as=True)
                qapp.processEvents()

            assert proj_file.is_file()
            proj_text = proj_file.read_text(encoding="utf-8")
            # 验证绝无 token 泄漏
            assert token not in proj_text
            assert "gui_bearer_prof" in proj_text

            # 在全新 MainWindow 实例中重开项目，验证凭据 Profile 引用被正确恢复
            win2 = MainWindow()
            win2.show()
            qapp.processEvents()
            try:
                win2.open_project(proj_file)
                start_t = time.monotonic()
                while time.monotonic() - start_t < deadline:
                    qapp.processEvents()
                    if len(win2.layer_panel.layers) > 0:
                        break
                    time.sleep(0.05)

                assert len(win2.layer_panel.layers) == 1
                restored_ly = win2.layer_panel.layers[0]
                assert restored_ly.credential_profile == "gui_bearer_prof"
            finally:
                win2.close()
        finally:
            win.close()
            server.stop()
