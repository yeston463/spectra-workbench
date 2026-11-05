"""同一容器内子数据集切换的请求上下文隔离回归测试（第 24 轮）。

复核指出：`_on_subdataset_selected` 的上下文只有（项目代际, 图层 ID, 源文件），
而同一容器内的子数据集 A/B **共用这三项**，因此无法区分；
`info['uri'] == uri` 只比较「响应」与「原请求」，并未验证它是否仍是**当前选择**。

本文件用**确定性反向投递**覆盖以下场景（成功与错误路径分别覆盖，
不用 sleep 代替断言）：

- A→B：迟到的 A 成功/错误都不得覆盖 B；
- A→B→A：第一轮 A 的结果不得覆盖最后一轮 A（只能靠请求代际区分）；
- 同 URI 重试：以最后一次请求为准；
- 清空数据源后迟到；
- 移除图层后迟到；
- 关窗后迟到。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

pytestmark = [pytest.mark.integration, pytest.mark.gui]

URI_A = 'NETCDF:"fake_container.nc":A'
URI_B = 'NETCDF:"fake_container.nc":B'


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


class _SpyClient:
    """记录子数据集探测请求的异步客户端 spy。"""

    _closed = False
    is_alive = False

    def __init__(self) -> None:
        self.pending: list[tuple[str, object, object]] = []

    def probe_vector_async(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return "req"

    def list_subdatasets_async(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return "req"

    def probe_subdataset_async(self, uri, on_success=None, on_error=None):  # noqa: ANN001, ANN201
        self.pending.append((uri, on_success, on_error))
        return "req"

    def read_window_async(self, **kwargs):  # noqa: ANN003, ANN201
        return "req"

    def stop(self, timeout: float = 1.0) -> int:  # noqa: ANN001
        self._closed = True
        return 0


def _info(uri: str, width: int) -> dict:
    return {
        "uri": uri,
        "width": width,
        "height": 1,
        "count": 1,
        "driver": "spy",
        "bands": [],
    }


@pytest.fixture
def window(qapp: QApplication, tmp_path: Path):
    win = MainWindow(project_root=tmp_path)
    win.show()
    client = _SpyClient()
    win._clients["gdal"] = client
    layer = LayerItem(
        "container",
        tmp_path / "fake_container.nc",
        {"width": 2, "height": 2, "count": 1},
    )
    win.layer_panel.add_layer(layer, select=True)
    yield win, client, layer
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _load_two_subdatasets(win: MainWindow) -> None:
    """在面板中装载同一容器的两个子数据集 A 与 B。"""
    win.metadata_panel.show_subdatasets(
        {
            "subdataset_count": 2,
            "subdatasets": [
                {"uri": URI_A, "description": "A"},
                {"uri": URI_B, "description": "B"},
            ],
        }
    )


def _requests(client: _SpyClient) -> dict[str, list[tuple[object, object]]]:
    """按 URI 归集请求（保留发起顺序，便于投递第一轮/最后一轮）。"""
    out: dict[str, list[tuple[object, object]]] = {}
    for uri, ok, err in client.pending:
        out.setdefault(uri, []).append((ok, err))
    return out


def test_late_a_success_cannot_overwrite_b(window) -> None:
    """A→B：迟到的 A 成功响应不得覆盖 B 的详情。"""
    win, client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(1)  # 选中 B
    qapp_process = win.metadata_panel
    assert qapp_process.current_subdataset_uri() == URI_B

    reqs = _requests(client)
    reqs[URI_B][0][0](_info(URI_B, 222))
    assert "222" in win.metadata_panel.lbl_subdataset_detail.text()

    reqs[URI_A][0][0](_info(URI_A, 111))
    assert "222" in win.metadata_panel.lbl_subdataset_detail.text(), "迟到的 A 成功响应不得覆盖 B"
    assert "111" not in win.metadata_panel.lbl_subdataset_detail.text()


def test_late_a_error_cannot_overwrite_b(window) -> None:
    """A→B：迟到的 A 错误响应也不得覆盖 B 的详情（错误路径同样受保护）。"""
    win, client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(1)

    reqs = _requests(client)
    reqs[URI_B][0][0](_info(URI_B, 222))
    assert "222" in win.metadata_panel.lbl_subdataset_detail.text()

    reqs[URI_A][0][1]("late A error")
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "late A error" not in detail, "迟到的 A 错误响应不得覆盖 B"
    assert "222" in detail, "被丢弃的响应不得清空当前正确内容"


def test_a_b_a_first_round_a_cannot_overwrite_last_round_a(window) -> None:
    """A→B→A：第一轮 A 的结果不得覆盖最后一轮 A。

    第一轮 A 与最后一轮 A 的 URI 完全相同，只有请求代际能区分。
    """
    win, client, _layer = window
    _load_two_subdatasets(win)

    # 第一轮：选 A
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)
    # 第二轮：选 B
    win.metadata_panel.cmb_subdataset.setCurrentIndex(1)
    # 第三轮：再选 A
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)
    assert win.metadata_panel.current_subdataset_uri() == URI_A

    reqs = _requests(client)
    a_requests = reqs[URI_A]
    assert len(a_requests) >= 2, f"A 至少应被请求两次，实际 {len(a_requests)} 次"

    # 最后一轮 A 先返回
    a_requests[-1][0](_info(URI_A, 333))
    assert "333" in win.metadata_panel.lbl_subdataset_detail.text()

    # 第一轮 A 迟到：不得覆盖最后一轮 A
    a_requests[0][0](_info(URI_A, 111))
    assert "333" in win.metadata_panel.lbl_subdataset_detail.text(), "第一轮 A 不得覆盖最后一轮 A"
    assert "111" not in win.metadata_panel.lbl_subdataset_detail.text()

    # 第一轮 A 的迟到错误同样不得覆盖
    a_requests[0][1]("late first-round A error")
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "late first-round A error" not in detail
    assert "333" in detail


def test_same_uri_retry_uses_latest_request(window) -> None:
    """同 URI 重复探测：以最后一次请求为准。

    通过直接再次选择同一 URI 触发重复探测（combo 的 setCurrentIndex 在索引未变时
    不发信号，无法表达「同一项被再次探测」）。
    """
    win, client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)

    before = len(_requests(client).get(URI_A, []))
    win._on_subdataset_selected(URI_A)
    a_requests = _requests(client)[URI_A]
    assert len(a_requests) == before + 1, "同 URI 应被再次请求"

    a_requests[-1][0](_info(URI_A, 555))
    assert "555" in win.metadata_panel.lbl_subdataset_detail.text()

    a_requests[0][0](_info(URI_A, 111))
    assert "555" in win.metadata_panel.lbl_subdataset_detail.text(), "同 URI 旧请求不得覆盖新请求"

    a_requests[0][1]("late same-uri error")
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "late same-uri error" not in detail
    assert "555" in detail


def test_late_response_after_clearing_subdatasets_is_dropped(window) -> None:
    """清空数据源后，迟到响应不得改写详情。"""
    win, client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)

    reqs = _requests(client)
    win.metadata_panel.clear_subdatasets("已清空")

    reqs[URI_A][0][0](_info(URI_A, 111))
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "111" not in detail, "清空后的迟到响应不得改写详情"
    assert "已清空" in win.metadata_panel.lbl_subdataset_summary.text()


def test_late_response_after_layer_removal_is_dropped(window) -> None:
    """移除图层后，迟到响应不得改写详情。"""
    win, client, layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)

    reqs = _requests(client)
    win._on_layer_removed(layer.layer_id)

    reqs[URI_A][0][0](_info(URI_A, 111))
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "111" not in detail, "移除图层后的迟到响应不得改写详情"


def test_late_response_after_close_is_dropped(window) -> None:
    """关窗后，迟到响应不得改写详情（也不得抛异常）。"""
    win, client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)

    reqs = _requests(client)
    win.close()

    reqs[URI_A][0][0](_info(URI_A, 111))
    detail = win.metadata_panel.lbl_subdataset_detail.text()
    assert "111" not in detail, "关窗后的迟到响应不得改写详情"

    # 错误路径同样
    win._on_subdataset_selected(URI_B)
    reqs2 = _requests(client)
    reqs2[URI_B][-1][1]("late error after close")
    assert "late error after close" not in win.metadata_panel.lbl_subdataset_detail.text()


def test_metadata_panel_current_uri_reflects_selection(window) -> None:
    """面板必须能报告当前选中的 URI（回调校验依赖它）。"""
    win, _client, _layer = window
    _load_two_subdatasets(win)
    win.metadata_panel.cmb_subdataset.setCurrentIndex(1)
    assert win.metadata_panel.current_subdataset_uri() == URI_B
    win.metadata_panel.cmb_subdataset.setCurrentIndex(0)
    assert win.metadata_panel.current_subdataset_uri() == URI_A
