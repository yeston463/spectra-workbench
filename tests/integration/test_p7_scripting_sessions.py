"""P7-02 隔离脚本会话端到端验收。

验收标准（BACKLOG P7-02「GDAL/Rasterio 会话分别导入原生 API；停止、日志、成果登记和会话重启可用」）：
- GDAL 会话在**真实 GDAL 解释器**里 ``from osgeo import gdal`` 并创建/读回 GTiff；
- Rasterio 会话在 **sw-rasterio** 解释器里 ``import rasterio``，独立读回 GDAL 写；
- ``stdout.log`` / ``stderr.log`` 捕获日志； ``artifacts.json`` 登记 SHA-256；
- **stop()** 让 ``is_alive`` 变 False；下一轮 ``run()`` 自动开新会话（会话 ID 不变但进程重启）；
- session.json 记录 engine 与 python_path（可审计）；
- 未知 engine / 空 script 明确拒绝。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from spectra_workbench.scripting.session import (
    SESSION_ENGINES,
    ScriptSession,
    SessionError,
    _resolve_engine_python,
)

pytestmark = [pytest.mark.integration]

ROOT_PATH = None


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("p7-02")
    from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime

    proj_root = Path(find_project_root())
    return {
        "root": root,
        "proj_root": proj_root,
        "gdal_python": resolve_gdal_runtime(proj_root)["python"],
        "rio_python": proj_root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe",
    }


def test_engine_resolves_env_paths(world: dict[str, Any]) -> None:
    for engine in SESSION_ENGINES:
        py = _resolve_engine_python(engine, world["proj_root"])
        assert py.exists(), f"{engine} 解释器缺失：{py}"
    with pytest.raises(SessionError):
        _resolve_engine_python("scipy", world["proj_root"])

    gdal_session = ScriptSession("gdal", session_dir=world["root"] / "gdal-1")
    rio_session = ScriptSession("rasterio", session_dir=world["root"] / "rio-1")
    assert gdal_session.python_path == world["gdal_python"]
    assert rio_session.python_path == world["rio_python"]
    assert gdal_session.engine == "gdal"
    assert rio_session.engine == "rasterio"
    for session in (gdal_session, rio_session):
        info = session.info()
        assert (info["engine"], info["alive"]) in (("gdal", False), ("rasterio", False))
        assert "python_path" in info


def test_gdal_session_imports_native_and_writes_artifact(
    world: dict[str, Any], tmp_path: Path
) -> None:
    session_dir = world["root"] / "gdal-gui"
    session = ScriptSession("gdal", session_dir=session_dir)
    artifact_path = session_dir / "artifacts" / "gdal_write.tif"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    script = f"""
from osgeo import gdal, osr
import numpy as np
import sys, json

gdal.UseExceptions()
driver = gdal.GetDriverByName("GTiff")
ds = driver.Create(r"{artifact_path}", 3, 2, 1, gdal.GDT_Float32)
assert ds is not None
ds.SetGeoTransform((1000.0, 10.0, 0.0, 2000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteArray(np.array([[1.5, 2.5, 3.5], [4.5, 5.5, 6.5]]))
band.SetNoDataValue(-9999.0)
band.FlushCache()
ds = None
print("gdal native api ok")
sys.exit(0)
"""
    payload = session.run(script)
    assert payload["exit_code"] == 0, payload["stderr_tail"]
    assert "gdal native api ok" in payload["stdout_tail"]
    assert len(payload["artifacts"]) == 0  # 脚本没有显式 register_artifact

    assert artifact_path.exists(), "真实 GTiff 必须写盘"
    header = artifact_path.read_bytes()[:4]
    assert header == b"II*\x00", "GTiff 必须写盘成功"

    session_json = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert session_json["engine"] == "gdal"
    assert Path(session_json["python_path"]) == world["gdal_python"]
    assert (session_dir / "logs" / "stdout.log").exists()
    assert (session_dir / "logs" / "stderr.log").exists()
    assert (session_dir / "logs" / "stdout.log").read_text(encoding="utf-8").endswith(
        "\n"
    )


def test_rasterio_session_reads_native_and_registers_artifacts(
    world: dict[str, Any], tmp_path: Path
) -> None:
    # 先用 GDAL 会话写一个 GTiff
    session_dir = world["root"] / "gdal-produce"
    gdal_session = ScriptSession("gdal", session_dir=session_dir)
    seed_raster = session_dir / "artifacts" / "seed.tif"
    seed_raster.parent.mkdir(parents=True, exist_ok=True)
    produce_script = f"""
from osgeo import gdal
import numpy as np
import sys
gdal.UseExceptions()
driver = gdal.GetDriverByName("GTiff")
ds = driver.Create(r"{seed_raster}", 2, 2, 1, gdal.GDT_Float32)
ds.SetGeoTransform((0.0, 1.0, 0.0, 4.0, 0.0, -1.0))
band = ds.GetRasterBand(1)
band.WriteArray(np.array([[1.0, 2.0], [3.0, 4.0]]))
band.SetNoDataValue(-9999.0)
band.FlushCache()
ds = None
sys.exit(0)
"""
    produce = gdal_session.run(produce_script)
    assert produce["exit_code"] == 0, produce["stderr_tail"]
    assert seed_raster.exists()

    # rasterio 的 sw-rasterio 解释器读取 GDAL 写的文件并登记 artifact
    rio_dir = world["root"] / "rio-session"
    rio_session = ScriptSession("rasterio", session_dir=rio_dir)
    read_script = f"""
import rasterio
import sys

with rasterio.open(r"{seed_raster}") as src:
    assert src.width == 2, src.width
    assert src.height == 2, src.height
    assert src.count == 1, src.count
    arr = src.read(1)
    assert arr.shape == (2, 2), arr.shape
    print("rasterio native api ok; nodata=%r" % (src.nodata,))

# 显式登记成果
register_artifact(r"{seed_raster}")
sys.exit(0)
"""
    rio_payload = rio_session.run(read_script)
    assert rio_payload["exit_code"] == 0, rio_payload["stderr_tail"]
    assert "rasterio native api ok; " in rio_payload["stdout_tail"]

    # 成果登记必须含 SHA-256
    assert len(rio_payload["artifacts"]) == 1
    artifact = rio_payload["artifacts"][0]
    assert Path(artifact["path"]) == seed_raster
    assert artifact["sha256"] == hashlib.sha256(seed_raster.read_bytes()).hexdigest()
    assert artifact["size"] == seed_raster.stat().st_size
    assert (rio_dir / "artifacts.json").exists()


def test_stop_and_restart_session(world: dict[str, Any]) -> None:
    session_dir = world["root"] / "restart-suite"
    session = ScriptSession("gdal", session_dir=session_dir)

    # run() 结束后 proc 返回 None；is_alive False
    payload = session.run("print('hello-1')")
    assert session.is_alive is False
    assert payload["exit_code"] == 0

    # 显式重复 stop() 应当幂等
    assert session.stop() == 0
    assert session.is_alive is False

    # 重启：同一 session_dir 里再跑一次，产物存放在同一 logs/stdout.log
    payload2 = session.run("print('hello-2')")
    assert payload2["exit_code"] == 0
    stdout_path = session.session_dir / "logs" / "stdout.log"
    text = stdout_path.read_text(encoding="utf-8")
    # 上一轮 stdout.log 会被清（清晰划分日志归属）；仅保留本轮
    assert text.strip() == "hello-2"
    assert "hello-1" not in text


def test_start_unknown_engine_or_empty_script_is_rejected(world: dict[str, Any]) -> None:
    with pytest.raises(SessionError):
        ScriptSession("scipy")
    session = ScriptSession("gdal", session_dir=world["root"] / "empty-script")
    with pytest.raises(SessionError, match="script 必须"):
        session.run("")
    with pytest.raises(SessionError, match="script 必须"):
        session.run("   \n\t")
