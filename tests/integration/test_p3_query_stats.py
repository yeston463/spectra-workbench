"""P3-05 / P3-02 / P3-03 的引擎侧跨进程验收测试。

期望值一律来自样例生成时的解析公式（ramp/spike 定义），不是把实现输出当参考值。
坐标与投影数学全部由 worker 完成，测试进程不导入 osgeo。
"""

from __future__ import annotations

import base64
from pathlib import Path

import numpy as np
import pytest

from spectra_workbench.workers.process import WorkerProcess

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def samples() -> dict:
    from tests.fixtures.p3_env import ensure_p3_samples

    return ensure_p3_samples()


@pytest.fixture(scope="module")
def worker() -> WorkerProcess:
    wp = WorkerProcess(provider_id="gdal")
    wp.start(timeout=40)
    try:
        yield wp
    finally:
        wp.stop(timeout=5)


def ramp_expect(band: int, col: int) -> int:
    return (col * band) % 60000


# ------------------------------------------------------------------ 元数据准确性


def test_probe_reports_spatial_model_and_overviews(worker: WorkerProcess, samples: dict) -> None:
    rot = samples["samples"]["rotated"]
    meta = worker.probe_dataset(rot["path"])
    assert meta["width"] == 200 and meta["height"] == 150 and meta["count"] == 3
    assert meta["spatial_model"] == "affine"
    assert meta["has_geotransform"] is True
    assert meta["geotransform"] == rot["geotransform"], "旋转系数必须原样回报"
    assert meta["crs_epsg"] == 32650
    assert meta["overview_levels"], "有概览必须报出来"
    assert [s["width"] for s in meta["bands"][0]["overview_sizes"]] == [100, 50, 25]
    assert meta["bands"][0]["nodata"] == 65535.0


def test_probe_distinguishes_no_crs_and_no_geotransform(
    worker: WorkerProcess, samples: dict
) -> None:
    no_crs = worker.probe_dataset(samples["samples"]["no_crs"]["path"])
    assert no_crs["crs_wkt"] is None and no_crs["crs_epsg"] is None
    assert no_crs["has_geotransform"] is True, "无 CRS 但有像元坐标参考，是两种不同缺口"

    no_gt = worker.probe_dataset(samples["samples"]["no_gt"]["path"])
    assert no_gt["has_geotransform"] is False
    assert no_gt["spatial_model"] == "none", "不得把 GDAL 默认值当成地理参考"


def test_probe_reports_mask_alpha_and_scale_offset(worker: WorkerProcess, samples: dict) -> None:
    masked = worker.probe_dataset(samples["samples"]["masked"]["path"])
    b1 = masked["bands"][0]
    assert b1["nodata"] is None, "该样例只有 mask 没有 NoData"
    # 标志位由 GDAL 决定（本机构建返回 GMF_NODATA 位），真正的契约是掩膜值能被读到
    assert b1["mask_flags"] != 1, "掩膜样例不应报告 GMF_ALL_VALID"

    alpha = worker.probe_dataset(samples["samples"]["alpha"]["path"])
    assert alpha["count"] == 4
    assert any(bool(b["has_alpha"]) for b in alpha["bands"])

    phys = worker.probe_dataset(samples["samples"]["scale_offset"]["path"])
    assert phys["bands"][0]["scale"] == pytest.approx(1e-4)
    assert phys["bands"][0]["offset"] == pytest.approx(0.5)


# ------------------------------------------------------------------ 坐标查询


def test_query_pixel_separates_nodata_valid_zero_and_mask(
    worker: WorkerProcess, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    r0, c0, r1, c1 = rot["nodata_block"]
    q_nd = worker.query_pixel(rot["path"], row=r0 + 1, col=c0 + 1)
    assert q_nd["bands"][0]["status"] == "nodata", q_nd["bands"]
    assert q_nd["bands"][0]["raw"] == 65535.0

    q_zero = worker.query_pixel(rot["path"], row=3, col=0)
    assert q_zero["bands"][0]["status"] == "valid"
    assert q_zero["bands"][0]["raw"] == 0.0
    assert q_zero["bands"][0]["is_zero"] is True, "合法 0 值必须被标成零，而不是无效"

    mrec = samples["samples"]["masked"]
    mr0, mc0, mr1, mc1 = mrec["mask_block"]
    q_masked = worker.query_pixel(mrec["path"], row=mr0 + 2, col=mc0 + 2)
    assert q_masked["bands"][0]["status"] == "masked", q_masked["bands"]
    assert q_masked["bands"][0]["raw"] == float(ramp_expect(1, mc0 + 2)), "被掩膜像元的值本身合法"

    arec = samples["samples"]["alpha"]
    ar0, ac0, ar1, ac1 = arec["alpha_block"]
    q_alpha = worker.query_pixel(arec["path"], row=ar0 + 1, col=ac0 + 1, bands=[1])
    assert q_alpha["bands"][0]["status"] == "alpha_zero", q_alpha["bands"]
    q_alpha_ok = worker.query_pixel(arec["path"], row=40, col=40, bands=[1])
    assert q_alpha_ok["bands"][0]["status"] == "valid"

    q_out = worker.query_pixel(rot["path"], row=999, col=999)
    assert q_out["inside"] is False and q_out["bands"][0]["status"] == "outside"


def test_query_pixel_values_match_generator_formula(worker: WorkerProcess, samples: dict) -> None:
    rot = samples["samples"]["rotated"]
    for row, col in [(7, 13), (50, 77), (149, 199)]:
        q = worker.query_pixel(rot["path"], row=row, col=col)
        for i, band in enumerate(q["bands"], start=1):
            assert band["raw"] == float(ramp_expect(i, col)), (row, col, i, band["raw"])


def test_query_pixel_physical_uses_scale_offset(worker: WorkerProcess, samples: dict) -> None:
    path = samples["samples"]["scale_offset"]["path"]
    q = worker.query_pixel(path, row=5, col=7)
    raw = q["bands"][0]["raw"]
    assert raw == float(ramp_expect(1, 7))
    assert q["bands"][0]["physical"] == pytest.approx(raw * 1e-4 + 0.5)


def test_map_to_pixel_roundtrip_on_rotated_affine(worker: WorkerProcess, samples: dict) -> None:
    path = samples["samples"]["rotated"]["path"]
    for row, col in [(0, 0), (5, 7), (73, 141), (149, 199)]:
        q = worker.query_pixel(path, row=row, col=col)
        for key in ("map_center", "map_corner"):
            back = worker.map_to_pixel(path, q[key][0], q[key][1], convention="corner")
            assert (back["col"], back["row"]) == (col, row), (
                key,
                row,
                col,
                back["col"],
                back["row"],
            )


def test_pixel_center_and_corner_conventions_are_documented(
    worker: WorkerProcess, samples: dict
) -> None:
    """角点约定 floor(u)，中心约定 floor(u+0.5)；差异只应出现在 frac>=0.5 时。"""
    path = samples["samples"]["rotated"]["path"]
    gt = samples["samples"]["rotated"]["geotransform"]

    def to_map(u: float, v: float) -> tuple[float, float]:
        return (gt[0] + gt[1] * u + gt[2] * v, gt[3] + gt[4] * u + gt[5] * v)

    for (u, v), expect in [((20.25, 10.25), (20, 20)), ((20.75, 10.75), (20, 21))]:
        x, y = to_map(u, v)
        got = worker.map_to_pixel(path, x, y)
        assert got["col_corner"] == expect[0], (u, v, got["col_corner"])
        assert got["col_center"] == expect[1], (u, v, got["col_center"])
        assert got["inside"] is True


def test_map_to_pixel_refuses_dataset_without_geotransform(
    worker: WorkerProcess, samples: dict
) -> None:
    with pytest.raises(RuntimeError, match="GeoTransform"):
        worker.map_to_pixel(samples["samples"]["no_gt"]["path"], 1.0, 1.0)


def test_cross_crs_query_returns_same_pixel(worker: WorkerProcess, samples: dict) -> None:
    """真正的跨 CRS：像元中心在 4326 下的坐标换算到 32650 后，按视图 CRS 反算必须落回同一像元。"""
    path = samples["samples"]["geographic"]["path"]
    q = worker.query_pixel(path, row=5, col=10)
    assert q["crs_epsg"] == 4326
    lon, lat = q["map_center"]

    wkt_4326 = worker.crs_wkt(4326)["wkt"]
    wkt_32650 = worker.crs_wkt(32650)["wkt"]
    pt = worker.transform_point(lon, lat, wkt_4326, wkt_32650)
    east, north = pt["dst"]
    assert abs(east) > 1e5 and abs(north) > 1e6, f"32650 下应为大数值坐标：{pt['dst']}"

    back = worker.map_to_pixel(path, east, north, view_crs_wkt=wkt_32650, convention="corner")
    assert (back["col"], back["row"]) == (10, 5), back

    # 视图 CRS 查询时，返回的地图坐标应已是视图 CRS 下的值
    q_view = worker.query_pixel(path, row=5, col=10, view_crs_wkt=wkt_32650)
    assert q_view["map_center"] == pytest.approx([east, north], rel=1e-6)

    tb = worker.transform_bounds(path, wkt_32650)
    assert len(tb["bounds"]) == 4 and tb["dataset_crs_wkt"] == q["crs_wkt"]


# ------------------------------------------------------------------ 统计语义


def test_stats_sampled_is_labelled_and_full_is_exact(worker: WorkerProcess, samples: dict) -> None:
    path = samples["samples"]["rotated"]["path"]
    sampled = worker.compute_stats(path, mode="sampled", bands=[1], max_samples=200)
    assert sampled["mode"] == "sampled"
    assert sampled["source"] == "sampled_grid"
    assert sampled["stride"] > 1
    assert sampled["bands"][0]["total_count"] < 200 * 150, "抽样统计的像元总数必须小于全幅"

    full = worker.compute_stats(path, mode="full", bands=[1])
    assert full["source"] == "full_raster" and full["stride"] == 1
    assert full["bands"][0]["total_count"] == 200 * 150
    r0, c0, r1, c1 = samples["samples"]["rotated"]["nodata_block"]
    nd_count = (r1 - r0) * (c1 - c0)
    assert full["bands"][0]["valid_count"] == 200 * 150 - nd_count


def test_stats_full_is_refused_beyond_limit_not_silently_downgraded(
    worker: WorkerProcess, samples: dict
) -> None:
    with pytest.raises(RuntimeError, match="拒绝"):
        worker.compute_stats(samples["samples"]["rotated"]["path"], mode="full", full_limit=1000)


def test_percentile_stats_ignore_isolated_spike(worker: WorkerProcess, samples: dict) -> None:
    st = worker.compute_stats(samples["samples"]["spike"]["path"], mode="full", bands=[1])
    b = st["bands"][0]
    assert b["max"] == 60000.0
    assert b["p98"] < 200.0, "98 分位不应被孤立尖峰拉高"


# ------------------------------------------------------------------ 窗口读取


def _decode(payload: dict) -> np.ndarray:
    nb, h, w = (int(v) for v in payload["shape"])
    return np.frombuffer(
        base64.b64decode(payload["data_b64"]), dtype=payload["numpy_dtype"]
    ).reshape(nb, h, w)


def _valid(payload: dict) -> np.ndarray:
    nb, h, w = (int(v) for v in payload["shape"])
    bits = np.unpackbits(
        np.frombuffer(base64.b64decode(payload["valid_bits_b64"]), dtype=np.uint8),
        count=int(payload["valid_bits_count"]),
    )
    return bits.astype(bool).reshape(nb, h, w)


def test_read_window_raw_preserves_dtype_and_values(worker: WorkerProcess, samples: dict) -> None:
    path = samples["samples"]["rotated"]["path"]
    payload = worker.read_window(
        path, x_off=0, y_off=0, win_w=64, win_h=48, out_w=64, out_h=48, bands=[1, 2, 3], raw=True
    )
    assert payload["raw"] is True
    arr = _decode(payload)
    assert arr.dtype == np.uint16, "raw 模式必须保留原始 dtype，而不是预先拉伸成 uint8"
    for band_idx in range(3):
        for col in (0, 5, 31):
            expect = ramp_expect(band_idx + 1, col)
            assert int(arr[band_idx][3][col]) == expect, (band_idx, col, arr[band_idx][3][col])


def test_read_window_raw_marks_nodata_and_masked_invalid_but_keeps_zero(
    worker: WorkerProcess, samples: dict
) -> None:
    rot = samples["samples"]["rotated"]
    r0, c0, r1, c1 = rot["nodata_block"]
    payload = worker.read_window(
        rot["path"], x_off=c0, y_off=r0, win_w=8, win_h=8, out_w=8, out_h=8, bands=[1], raw=True
    )
    valid = _valid(payload)
    assert not valid[0][1, 1], "NoData 像元必须无效"

    qzero = worker.read_window(
        rot["path"], x_off=0, y_off=0, win_w=16, win_h=16, out_w=16, out_h=16, bands=[1], raw=True
    )
    assert _valid(qzero)[0][3, 0], "值为 0 的合法像元必须有效"

    mrec = samples["samples"]["masked"]
    mr0, mc0, mr1, mc1 = mrec["mask_block"]
    mp = worker.read_window(
        mrec["path"],
        x_off=mc0 - 10,
        y_off=mr0 - 10,
        win_w=32,
        win_h=32,
        out_w=32,
        out_h=32,
        bands=[1],
        raw=True,
    )
    mvalid = _valid(mp)
    assert not mvalid[0][10 + 2, 10 + 2], "掩膜区必须无效"
    assert mvalid[0][30, 30], "掩膜外必须有效"


def test_overview_is_used_only_when_decimating(worker: WorkerProcess, samples: dict) -> None:
    path = samples["samples"]["rotated"]["path"]
    full = worker.read_window(
        path, x_off=0, y_off=0, win_w=200, win_h=150, out_w=200, out_h=150, bands=[1], raw=True
    )
    assert full["overview_used"] == -1, "1:1 读取不得用概览"

    small = worker.read_window(
        path, x_off=0, y_off=0, win_w=200, win_h=150, out_w=25, out_h=19, bands=[1], raw=True
    )
    assert small["overview_used"] >= 0, f"需要 8 倍降采样时应使用概览：{small}"
    assert small["overview_ratio"] > 1.0
    assert small["needed_decimation"] == pytest.approx(200 / 25)


def test_read_view_window_reprojects_into_common_grid(worker: WorkerProcess, samples: dict) -> None:
    """跨 CRS 叠加：重投影到视图网格后，已知地物必须落在可解释的网格位置。"""
    path = samples["samples"]["geographic"]["path"]
    wkt_32650 = worker.crs_wkt(32650)["wkt"]
    tb = worker.transform_bounds(path, wkt_32650)
    x0, y0, x1, y1 = tb["bounds"]
    payload = worker.read_view_window(
        path,
        view_crs_wkt=wkt_32650,
        x_min=x0,
        y_min=y0,
        x_max=x1,
        y_max=y1,
        out_w=64,
        out_h=64,
        bands=[1],
        resampling="near",
    )
    assert payload["reprojected"] is True
    grid = payload["grid"]
    assert grid["out_w"] == 64 and grid["affine"] is not None
    arr = _decode(payload)
    assert arr.shape == (1, 64, 64)
    # 视图网格仿射必须与请求范围一致
    aff = grid["affine"]
    assert aff[0] == pytest.approx(x0, rel=1e-6)
    assert aff[3] == pytest.approx(y1, rel=1e-6)
    assert aff[1] == pytest.approx((x1 - x0) / 64.0, rel=1e-6)
    assert aff[5] == pytest.approx(-(y1 - y0) / 64.0, rel=1e-6)
    # 数据集有地理参考，重投影后不应整幅全无效
    assert _valid(payload).sum() > 0


def test_invalid_band_request_is_reported_not_silently_clamped(
    worker: WorkerProcess, samples: dict
) -> None:
    with pytest.raises(RuntimeError, match="越界"):
        worker.read_window(samples["samples"]["rotated"]["path"], bands=[9], raw=True)


# ------------------------------------------------------------------ spill 清理


def test_spill_files_cleaned_after_success_and_stop(tmp_path: Path, samples: dict) -> None:
    """大窗口必须走 spill 文件；消费后与退出后目录里不应残留。"""
    import tempfile

    root = Path(tempfile.gettempdir()) / "spectra_workbench_spill"
    before = set(root.glob("worker-*")) if root.exists() else set()

    wp = WorkerProcess(provider_id="gdal")
    wp.start(timeout=40)
    try:
        # 2048x2048 x 3 波段 uint16 原始值 -> 远超 1 MiB 控制消息上限
        payload = wp.read_window(
            samples["samples"]["rotated"]["path"],
            x_off=0,
            y_off=0,
            win_w=200,
            win_h=150,
            out_w=2048,
            out_h=2048,
            bands=[1, 2, 3],
            raw=True,
            timeout=60,
        )
        assert payload["raw"] is True
        mine = [d for d in root.glob("worker-*") if d not in before]
        leftovers = [f for d in mine for f in d.glob("*.json")]
        assert leftovers == [], f"消费成功后 spill 未清理：{leftovers}"
    finally:
        wp.stop(timeout=5)

    still = [d for d in root.glob("worker-*") if d not in before]
    assert still == [], f"worker 退出后 spill 目录未清理：{still}"
