"""已知缩放因子下的真实高 DPI 渲染验收（P3-01 长期待验项收口）。

此前的 ``test_gui_exposes_actual_device_pixel_ratio`` 只证明 dpr **可被读取**，
既没设置也没实测缩放因子，因此不能作为高 DPI 验收。QApplication 是进程单例，
DPI 环境变量必须在它构造之前生效，故本测试用**独立子进程**在明确的
``QT_SCALE_FACTOR`` 下真实创建主窗口、渲染视图并回传观测值。

**结论边界**：这里证明的是「已知缩放因子下布局、截图与 QImage dpr 元数据正确」
以及「进程正常退出、无 QThreadStorage 警告」；物理显示器上的字体可读性
与多显示器 DPI 切换仍属人工/实机验收，不由本测试宣称。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spectra_workbench.app.doctor import find_project_root

pytestmark = [pytest.mark.integration, pytest.mark.gui]

ROOT = find_project_root()

_PROBE = r"""
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)
app = QApplication(["high-dpi-probe"])

from spectra_workbench.ui.main_window import MainWindow

win = MainWindow(project_root=Path(os.environ["SW_PROBE_ROOT"]))
win.show()
win.resize(1200, 800)
for _ in range(8):
    app.processEvents()

image = win.canvas.capture_view_image()
pixmap = win.grab()
out = {
    "dpr": float(win.devicePixelRatioF()),
    "logical_size": [win.width(), win.height()],
    "panel_width": win.layer_panel.width(),
    "panel_height": win.info_panel.height(),
    "panel_visible": bool(win.layer_panel.isVisible()),
    "grab_size": [pixmap.width(), pixmap.height()],
    "grab_dpr": float(pixmap.devicePixelRatioF()),
    "view_image_size": [image.width(), image.height()],
    "view_image_dpr": float(image.devicePixelRatioF()),
    "view_is_null": bool(image.isNull()),
}
print("DPI_RESULT=" + json.dumps(out), flush=True)
win.close()
win.deleteLater()
app.processEvents()
"""


def _run_probe(scale: float, project_root: Path) -> tuple[dict, subprocess.CompletedProcess[str]]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_SCALE_FACTOR"] = str(scale)
    env["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["SW_PROBE_ROOT"] = str(project_root)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(ROOT),
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("DPI_RESULT="):
            return json.loads(line[len("DPI_RESULT=") :]), proc
    raise AssertionError(
        f"高 DPI 探针未回传结果 (rc={proc.returncode})：\n"
        f"stdout={proc.stdout[-800:]}\nstderr={proc.stderr[-800:]}"
    )


@pytest.mark.parametrize("scale", [1.0, 2.0, 3.0])
def test_known_scale_factor_renders_correctly(tmp_path: Path, scale: float) -> None:
    """在明确的缩放因子下：dpr 生效、布局有效、截图按 dpr 出图、进程干净退出。"""
    data, proc = _run_probe(scale, tmp_path)

    assert proc.returncode == 0, (
        f"高 DPI 进程非零退出（可能是 finalization 崩溃）：{proc.stderr[-800:]}"
    )
    assert "QThreadStorage" not in proc.stderr, (
        f"高 DPI 进程出现 QThreadStorage 警告：{proc.stderr[-800:]}"
    )
    assert data["dpr"] == pytest.approx(scale, abs=0.01), (
        f"缩放因子未生效：期望 {scale}，实测 {data['dpr']}"
    )
    assert data["logical_size"][0] > 0 and data["logical_size"][1] > 0
    assert data["panel_width"] > 0 and data["panel_height"] > 0
    assert data["panel_visible"] is True, "高 DPI 下图层面板不得消失"

    assert data["view_is_null"] is False, "视图截图必须是真实渲染结果"
    assert data["view_image_size"][0] > 0 and data["view_image_size"][1] > 0
    # 渲染倍率 = max(2.0, dpr)，且必须写入 QImage 的 dpr 元数据
    assert data["view_image_dpr"] == pytest.approx(max(2.0, scale), abs=0.01)
    assert data["grab_dpr"] == pytest.approx(scale, abs=0.01)


def test_high_dpi_observations_are_scale_dependent(tmp_path: Path) -> None:
    """对照实验：不同缩放因子必须产生可观测差异，否则等于没测到 DPI。"""
    (tmp_path / "low").mkdir(exist_ok=True)
    (tmp_path / "high").mkdir(exist_ok=True)
    low, _ = _run_probe(1.0, tmp_path / "low")
    high, _ = _run_probe(3.0, tmp_path / "high")

    assert high["dpr"] > low["dpr"]
    assert high["grab_dpr"] > low["grab_dpr"]
    assert high["view_image_dpr"] > low["view_image_dpr"], (
        "高 DPI 下截图倍率必须随设备像素比提升，不能固定 2.0"
    )
    assert high["view_image_size"][0] >= low["view_image_size"][0]
    # 逻辑窗口尺寸不应被缩放因子撑大（缩放只影响物理像素）
    assert high["logical_size"] == low["logical_size"]
