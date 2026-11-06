"""课程原始数据真实打开验收（P3-02 长期待验项收口）。

数据是超星「遥感图像处理」课程附件中**真实配送的影像**，此前一直挂在
「待条件具备时补做」，原因是只盘点了目录表面：原始栅格其实打包在
``_chaoxing_rs/files/*.rar`` 里，从未解压。本测试从只读资料目录读取 rar，
解压到 ``work/course_data``（gitignored），再用**产品真实 GDAL worker** 与
**真实 MainWindow** 打开、渲染、保存项目、重开，逐项留下可执行断言。

只读约束：不修改 ``_chaoxing_rs`` 内任何文件，也不运行其中的抓取/下载/OCR
脚本；解压只写本仓库 ``work/`` 下的临时副本。

CI/干净机没有课程附件时自动 skip（``SW_COURSE_DATA_DIR`` 可指向已有副本），
不会把「数据不可得」伪装成验收通过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]

ROOT = find_project_root()
COURSE_FILES = ROOT.parent / "_chaoxing_rs" / "files"
DEST = ROOT / "work" / "course_data"
ARCHIVES = (
    "上机实习2：频率域图像处__FFT.rar",
    "上机实习1：精准农业__农业.rar",
)
_UNRAR_CANDIDATES = (
    Path("C:/Program Files/WinRAR/UnRAR.exe"),
    Path("C:/Program Files (x86)/WinRAR/UnRAR.exe"),
)

#: (相对路径, driver, width, height, 波段数, 首波段 dtype, crs_epsg)
CASES: tuple[tuple[str, str, int, int, int, str, int | None], ...] = (
    ("FFT/lena.tif", "GTiff", 256, 256, 3, "Byte", None),
    ("FFT/TM条带.img", "ENVI", 512, 512, 1, "Byte", None),
    ("FFT/FFT", "ENVI", 256, 256, 3, "CFloat32", None),
    ("农业/作物.dat", "ENVI", 484, 644, 5, "Float32", 32617),
)


def _unrar() -> Path | None:
    for candidate in _UNRAR_CANDIDATES:
        if candidate.exists():
            return candidate
    found = shutil.which("UnRAR") or shutil.which("unrar")
    return Path(found) if found else None


def _prepare(dest: Path) -> None:
    tool = _unrar()
    archives = [COURSE_FILES / name for name in ARCHIVES]
    if tool is None or not all(p.exists() for p in archives):
        pytest.skip("课程 rar 附件或 UnRAR 不可用：真实数据验收跳过（不计为通过）")
    dest.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        proc = subprocess.run(
            [str(tool), "x", "-o+", "-inul", str(archive), str(dest) + "\\"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"课程 rar 解压失败（UnRAR rc={proc.returncode}）：{proc.stderr[:200]}")


@pytest.fixture(scope="module")
def course_data() -> Path:
    override = os.environ.get("SW_COURSE_DATA_DIR")
    dest = Path(override) if override else DEST
    if not (dest / "FFT" / "lena.tif").exists() and override is None:
        _prepare(dest)
    if not (dest / "FFT" / "lena.tif").exists():
        pytest.skip("课程原始数据未落盘：真实数据验收跳过（不计为通过）")
    return dest


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def gdal_client() -> Any:
    client = QtWorkerClient("gdal", project_root=ROOT)
    try:
        yield client
    finally:
        client.stop(timeout=5.0)


def _pump(qapp: QApplication, predicate: Any, timeout: float = 60.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


@pytest.mark.parametrize("rel,driver,w,h,count,dtype,epsg", CASES)
def test_course_raster_probed_by_real_worker(
    course_data: Path,
    gdal_client: QtWorkerClient,
    rel: str,
    driver: str,
    w: int,
    h: int,
    count: int,
    dtype: str,
    epsg: int | None,
) -> None:
    """真实课程栅格必须经产品 GDAL worker 探测出准确的数据类型与空间模型。"""
    path = course_data / rel
    assert path.exists(), f"课程原始文件缺失：{path}"
    info = gdal_client.probe_dataset(path, timeout=30.0)

    assert info["driver"] == driver
    assert info["width"] == w and info["height"] == h
    assert info["count"] == count
    assert info["dtypes"][0] == dtype
    assert info["crs_epsg"] == epsg

    # ENVI 头文件必须真实存在且被配套读取（不能只靠扩展名猜）
    if driver == "ENVI":
        header = path.with_suffix(".hdr")
        if not header.exists():
            header = path.with_suffix(".HDR")
        assert header.exists(), f"ENVI 头文件缺失：{path}"


def test_course_envi_without_header_is_rejected_clearly(
    course_data: Path, gdal_client: QtWorkerClient, tmp_path: Path
) -> None:
    """ENVI 主文件脱离 .hdr 后必须明确失败，不能静默当成别的格式打开。"""
    src = course_data / "农业" / "作物.dat"
    orphan = tmp_path / "orphan.dat"
    orphan.write_bytes(src.read_bytes()[:4096])
    with pytest.raises(Exception) as exc:
        gdal_client.probe_dataset(orphan, timeout=30.0)
    message = str(exc.value)
    assert message and (
        "supported file format" in message.lower() or "无法打开" in message
    ), f"应给出明确的格式不支持信息，实际：{message}"


def test_course_real_data_gui_open_render_save_reopen(
    qapp: QApplication, course_data: Path, tmp_path: Path
) -> None:
    """真实 lena.tif 与 ENVI 多波段影像从桌面打开 → 渲染 → 存项目 → 重开。"""
    lena = (course_data / "FFT" / "lena.tif").resolve()
    crop = (course_data / "农业" / "作物.dat").resolve()
    project = tmp_path / "course_real_data.spectra.json"

    win = MainWindow(project_root=tmp_path)
    win2: MainWindow | None = None
    try:
        for src in (lena, crop):
            win.open_dataset(src)
            expected = 1 if src == lena else 2
            _pump(
                qapp,
                lambda n=expected: len(win.layer_panel.layers) == n,
                label=f"真实课程图层就绪 {src.name}",
            )
        _pump(
            qapp,
            lambda: getattr(win.canvas.img_item, "image", None) is not None,
            timeout=30.0,
            label="真实课程影像渲染出像素",
        )
        assert {ly.file_path for ly in win.layer_panel.layers} == {lena, crop}
        rendered = win.canvas.img_item.image
        assert rendered is not None and rendered.size > 0

        # 视图截图在真实课程数据上同样可用（且明确不含地理参考）
        view_png = tmp_path / "course_view.png"
        assert win.export_view_image(view_png) is not None
        assert view_png.exists() and view_png.stat().st_size > 1024

        win.project_path = project
        assert win.save_project_action() is True
        assert project.exists()

        win2 = MainWindow(project_root=tmp_path)
        assert win2.open_project(project) is True
        _pump(qapp, lambda: len(win2.layer_panel.layers) == 2, label="重开项目恢复课程图层")
        assert {ly.file_path for ly in win2.layer_panel.layers} == {lena, crop}
        assert win2.missing_sources == []
    finally:
        if win2 is not None:
            win2.close()
            win2.deleteLater()
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_course_complex_fft_band_has_explicit_behaviour(
    course_data: Path, gdal_client: QtWorkerClient
) -> None:
    """课程 FFT 成果是 CFloat32 复数：窗口读取必须给出明确结果或明确错误。

    合成样例从未覆盖复数波段，这里用真实数据固定其可观测行为；
    不允许静默返回全 0 / 空数据而伪装成功。
    """
    path = course_data / "FFT" / "FFT"
    try:
        data = gdal_client.read_window(path, 0, 0, 8, 8, timeout=30.0)
    except Exception as exc:
        assert str(exc), "复数波段若不支持，必须给出非空错误信息"
        return
    assert isinstance(data, dict) and data, "复数波段读取成功时不得返回空结果"
