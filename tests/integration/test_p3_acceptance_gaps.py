"""P3 原验收清单中两项此前缺少直接断言的验收测试。

依据 `docs/plan/02_里程碑与验收.md` 的原验收标准：

- **P3-01**「中文高 DPI 可用」：此前只有 `ensure_application_fonts` 的实现，
  没有直接断言「中文字体族可用」与「高 DPI 缩放下布局仍正常」的测试。
- **P3-03**「切换波段不出现旧图」：此前只有视图层过期响应的断言，
  没有直接断言「切换波段后旧波段的迟到响应不得覆盖画面、
  且新请求确实携带新波段」的测试。

按 AGENTS.md 第 9 条（`implemented` ≠ `done`），这类缺口必须有可执行的验收证据，
不能仅凭实现存在就宣称满足。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from spectra_workbench.rendering.cache import TileCache
from spectra_workbench.rendering.display import DisplayMode, DisplaySettings, StretchMode
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

pytestmark = [pytest.mark.integration, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


@pytest.fixture(scope="module")
def p3_samples() -> dict:
    from tests.fixtures.p3_env import ensure_p3_samples

    return ensure_p3_samples()


# ------------------------------------------------------ P3-01 中文高 DPI 可用
def test_chinese_font_family_is_available(qapp: QApplication) -> None:
    """中文字体族必须可用，避免文字渲染为方框或字形缺失。

    只断言「有可用的中文字体族」，不锁定具体字体名（不同 Windows 版本
    与 QGIS 环境下的字体集不一致）。
    """
    families = set(QFontDatabase.families())
    assert families, "字体库为空，中文与英文都会渲染为方框"

    chinese_hints = ("Microsoft YaHei", "微软雅黑", "SimSun", "宋体", "SimHei", "黑体")
    assert any(h in families for h in chinese_hints), (
        f"未找到可用的中文字体族；当前字体库样例：{sorted(families)[:20]}"
    )


def test_ensure_application_fonts_returns_usable_count(qapp: QApplication) -> None:
    """字体初始化必须返回可用的字体族数量（>0），否则视为不可用。"""
    count = ensure_application_fonts(qapp)
    assert count > 0, "ensure_application_fonts 未取得任何字体族"


def test_main_window_layout_geometry_is_valid(qapp: QApplication, tmp_path: Path) -> None:
    """主窗口与关键子控件的布局几何必须有效（非 0、非负）。

    **本项不构成高 DPI 验收**：它只在已创建的 QApplication 之后调整窗口尺寸，
    并未设置或测量实际缩放因子，因此只能证明「基础布局几何非零」，
    不能证明「高 DPI 缩放下的实际渲染与可读性」。后者保留待验。
    """
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        win.resize(1600, 1000)
        qapp.processEvents()

        assert win.width() > 0 and win.height() > 0, "窗口尺寸不得为 0"
        assert win.layer_panel.width() > 0, "图层面板宽度不得为 0"
        assert win.info_panel.height() > 0, "信息面板高度不得为 0"
        assert win.layer_panel.isVisible(), "图层面板应仍可见"
        assert win.layer_panel.width() >= 0 and win.info_panel.height() >= 0
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_gui_exposes_actual_device_pixel_ratio(qapp: QApplication, tmp_path: Path) -> None:
    """GUI 进程能读到**实际**的 devicePixelRatio（高 DPI 环境的前提）。

    本项证明「缩放因子可被测量」，为后续在已知缩放环境下补做高 DPI 验收
    提供可用的观测手段；**不**等同于高 DPI 可读性已验收。
    """
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        qapp.processEvents()
        dpr = win.devicePixelRatioF()
        assert dpr > 0, f"devicePixelRatioF 不得为非正数，实际 {dpr}"
        # 缩放因子必须能读出具体数值（而非只有布尔可用性）
        assert isinstance(dpr, float)
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


# ------------------------------------------ P3-03 切换波段不出现旧图
class _BandDelayClient:
    """可控的读取客户端：记录请求，按调用方指定的顺序投递响应。"""

    def __init__(self) -> None:
        self.pending: list[dict] = []

    def read_window_async(self, **kw):  # noqa: ANN003, ANN201
        self.pending.append(kw)

    def read_view_window_async(self, **kw):  # pragma: no cover - 未走该路径
        self.pending.append(kw)

    def transform_bounds_async(self, *a, **kw):  # pragma: no cover
        kw["on_success"]({"bounds": [0, 0, 1, 1]})


def _payload_for(kw: dict, band: int) -> dict:
    """构造一个能区分波段的合成响应。

    刻意用**非线性**波形 ``(j * band) % 251``：不同波段产生不同的空间分布，
    经 min-max 拉伸后仍不同（若用常量或线性波形，拉伸会把波段差异抹平，
    无法据此判断画布显示的是哪个波段）。
    """
    w, h = int(kw["out_w"]), int(kw["out_h"])
    cols = (np.arange(w, dtype=np.int64) * band) % 251
    arr = np.broadcast_to(cols[None, :], (1, h, w)).astype(np.uint16).copy()
    valid = np.ones((1, h, w), dtype=np.uint8)
    return {
        "raw": True,
        "x_off": kw["x_off"],
        "y_off": kw["y_off"],
        "win_w": kw["win_w"],
        "win_h": kw["win_h"],
        "out_w": w,
        "out_h": h,
        "bands": kw.get("bands") or [1],
        "shape": [1, h, w],
        "numpy_dtype": "uint16",
        "data_b64": base64.b64encode(arr.tobytes()).decode(),
        "valid_bits_b64": base64.b64encode(
            np.packbits(valid.reshape(-1), axis=None).tobytes()
        ).decode(),
        "valid_bits_count": int(valid.size),
        "band_meta": [
            {
                "band": 1,
                "dtype": "UInt16",
                "numpy_dtype": "uint16",
                "nodata": None,
                "scale": None,
                "offset": None,
                "mask_flags": 1,
                "has_alpha": False,
                "all_valid_mask": True,
                "min": 0.0,
                "max": 255.0,
                "valid_count": w * h,
                "total_count": w * h,
            }
        ],
        "overview_used": -1,
        "is_rgb": False,
    }


def test_switching_band_does_not_show_previous_band(qapp: QApplication) -> None:
    """切换波段后：旧波段的迟到响应不得覆盖画面，新请求必须携带新波段。

    用可控客户端注入响应，不依赖真实引擎与线程调度。
    """
    client = _BandDelayClient()
    canvas = ImageCanvas(cache=TileCache(budget_bytes=8 * 1024 * 1024))
    layer = LayerItem(
        name="bands",
        file_path=Path("synthetic.tif"),
        metadata={
            "width": 512,
            "height": 512,
            "count": 3,
            "driver": "GTiff",
            "spatial_model": "affine",
        },
    )
    try:
        canvas.set_layer(layer, client)
        assert client.pending, "首次渲染应发起一次读取请求"
        first_req = client.pending[-1]
        assert list(first_req.get("bands") or []) == [1, 2, 3], (
            f"初始为 RGB 三波段，实际 {first_req.get('bands')}"
        )

        # 切换到波段 3
        canvas.set_display(
            DisplaySettings(mode=DisplayMode.gray, gray_band=3, stretch=StretchMode.min_max)
        )
        assert len(client.pending) >= 2, "切换波段应发起新的读取请求"
        new_req = client.pending[-1]
        assert list(new_req.get("bands") or []) == [3], (
            f"新请求必须携带波段 3，实际 {new_req.get('bands')}"
        )

        # 先投递**旧波段**（RGB 三波段请求）的迟到响应——不得成为最终显示
        for kw in client.pending[:-1]:
            kw["on_success"](_payload_for(kw, band=1))
        qapp.processEvents()
        stale_value = _rendered_value(canvas)

        # 再投递**新波段**（波段 3）的响应——必须成为最终显示
        new_req["on_success"](_payload_for(new_req, band=3))
        qapp.processEvents()
        final_value = _rendered_value(canvas)

        # 「不出现旧图」的直接证据：旧波段响应被丢弃，画布上**根本没有**它的内容
        assert stale_value is None, (
            f"旧波段响应仍被渲染出来（值 {stale_value}），切换波段后出现了旧图"
        )
        # 最终显示必须是**新波段**那次响应的结果
        assert final_value is not None, "新波段响应投递后画布应渲染出像元"
        assert final_value == _expected_render_value(new_req, band=3), (
            f"最终显示不是新波段的结果：{final_value}"
        )
    finally:
        canvas.clear()
        canvas.close()
        qapp.processEvents()


def test_switching_band_invalidates_pending_response(qapp: QApplication) -> None:
    """切换波段必须推进画布代际，使在途的旧波段响应作废。"""
    client = _BandDelayClient()
    canvas = ImageCanvas(cache=TileCache(budget_bytes=8 * 1024 * 1024))
    layer = LayerItem(
        name="gen",
        file_path=Path("synthetic.tif"),
        metadata={
            "width": 256,
            "height": 256,
            "count": 3,
            "driver": "GTiff",
            "spatial_model": "affine",
        },
    )
    try:
        canvas.set_layer(layer, client)
        gen_before = canvas.generation
        canvas.set_display(
            DisplaySettings(mode=DisplayMode.gray, gray_band=2, stretch=StretchMode.min_max)
        )
        assert canvas.generation > gen_before, "切换波段必须推进画布代际"
    finally:
        canvas.clear()
        canvas.close()
        qapp.processEvents()


def _rendered_value(canvas) -> int | None:
    """读取画布当前渲染图像的代表性像元值（用于判断显示的是哪个波段）。"""
    img = canvas.img_item.image
    if img is None:
        return None
    arr = np.asarray(img)
    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.size == 0:
        return None
    return int(arr[arr.shape[0] // 2, arr.shape[1] // 2])


def _expected_render_value(kw: dict, band: int) -> int:
    """按与 _payload_for 相同的公式推导期望的渲染像元值。

    min-max 拉伸把 [min, max] 线性映射到 [0, 255]；这里只关心**相对关系**，
    因此用与实现一致的公式独立算出中心像元的目标值。
    """
    w = int(kw["out_w"])
    h = int(kw["out_h"])
    cols = (np.arange(w, dtype=np.int64) * band) % 251
    arr = np.broadcast_to(cols[None, :], (1, h, w)).astype(np.uint16)
    lo = float(arr.min())
    hi = float(arr.max())
    v = float(arr[0, h // 2, w // 2])
    if hi <= lo:
        return 0
    return int(round((v - lo) / (hi - lo) * 255.0))
