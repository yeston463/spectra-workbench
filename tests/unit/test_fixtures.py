"""P0-03 验收：小型合成数据与 ENVI 读写往返测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.fixtures.synthetic import (
    create_constant_sample,
    create_multiband_sample,
    create_nodata_sample,
    create_ramp_sample,
    generate_all_fixtures,
    read_envi_raster,
    write_envi_raster,
)

pytestmark = pytest.mark.unit


def test_envi_ramp_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "test_ramp"
        buf, stats = create_ramp_sample(16, 16, 0.0, 255.0)
        hdr, dat = write_envi_raster(base, buf, 16, 16, 1, "float32", description="Test Ramp")
        assert hdr.is_file()
        assert dat.is_file()

        parsed = read_envi_raster(hdr, dat)
        assert parsed["samples"] == 16
        assert parsed["lines"] == 16
        assert parsed["bands"] == 1
        assert parsed["dtype"] == "float32"
        vals = parsed["values"]
        assert len(vals) == 256
        assert vals[0] == pytest.approx(0.0)
        assert vals[15] == pytest.approx(255.0)
        assert min(vals) == pytest.approx(stats["min"])
        assert max(vals) == pytest.approx(stats["max"])


def test_envi_constant_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "test_const"
        buf, stats = create_constant_sample(8, 8, 42.0)
        hdr, dat = write_envi_raster(base, buf, 8, 8, 1, "float32")
        parsed = read_envi_raster(hdr, dat)
        vals = parsed["values"]
        assert len(vals) == 64
        assert all(v == pytest.approx(42.0) for v in vals)


def test_envi_multiband_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "test_mb"
        buf, stats = create_multiband_sample(8, 8, 4)
        hdr, dat = write_envi_raster(base, buf, 8, 8, 4, "uint16")
        parsed = read_envi_raster(hdr, dat)
        assert parsed["bands"] == 4
        assert parsed["dtype"] == "uint16"
        vals = parsed["values"]
        assert len(vals) == 8 * 8 * 4
        # First band starts at 1000
        assert vals[0] == 1000
        # Second band starts at 2000 (offset = 64)
        assert vals[64] == 2000
        # Third band starts at 3000 (offset = 128)
        assert vals[128] == 3000
        # Fourth band starts at 4000 (offset = 192)
        assert vals[192] == 4000


def test_envi_nodata_and_zero_separation():
    """工程约束 4：原始像元值、NoData、合法 0 值不得混淆。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "test_nd"
        buf, stats = create_nodata_sample(8, 8, -9999.0)
        hdr, dat = write_envi_raster(base, buf, 8, 8, 1, "float32", nodata_value=-9999.0)
        parsed = read_envi_raster(hdr, dat)
        vals = parsed["values"]

        # Line 0 is all NoData (-9999.0)
        for i in range(8):
            assert vals[i] == pytest.approx(-9999.0)

        # Line 1 even columns are legal 0.0, odd columns are positive numbers
        line1 = vals[8:16]
        assert line1[0] == pytest.approx(0.0)
        assert line1[1] == pytest.approx(11.0)
        assert line1[2] == pytest.approx(0.0)

        # Header metadata contains correct ignore value
        assert parsed["header"]["data ignore value"] == "-9999.0"


def test_generate_all_fixtures():
    with tempfile.TemporaryDirectory() as td:
        manifest = generate_all_fixtures(td)
        assert "ramp_16x16" in manifest["fixtures"]
        assert "constant_8x8" in manifest["fixtures"]
        assert "multiband_4b_8x8" in manifest["fixtures"]
        assert "nodata_masked_8x8" in manifest["fixtures"]
        p = Path(td)
        assert (p / "manifest.json").is_file()
        assert (p / "a01_ramp_16x16.hdr").is_file()
        assert (p / "a01_ramp_16x16.dat").is_file()
