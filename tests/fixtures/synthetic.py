"""小型合成数据集与 Fixture 生成器（P0-03，A01 样例库基线）。

设计原则：
1. 纯 Python 实现，零原生地理依赖，任何环境（甚至纯净 Python）均可运行。
2. 支持 ENVI 标准格式（.hdr 头文件 + .dat 扁平二进制文件）读写，满足遥感数据验收。
3. 提供统一数据模式：单波段 ramp、常量、多波段已知值、NoData/mask 像元。
4. 提供 manifest.json 清单，记录每个样本的尺寸、波段、dtype、已知数值特征与元数据。
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ENVI 数据类型代码定义 (官方标准):
# 1 = Byte (uint8)
# 2 = Integer (int16)
# 3 = Long Integer (int32)
# 4 = Floating-point (float32)
# 5 = Double-precision floating-point (float64)
# 12 = Unsigned integer (uint16)
ENVI_DTYPES = {
    "uint8": (1, "B", 1),
    "int16": (2, "h", 2),
    "int32": (3, "i", 4),
    "float32": (4, "f", 4),
    "float64": (5, "d", 8),
    "uint16": (12, "H", 2),
}


@dataclass
class SyntheticSampleMeta:
    name: str
    description: str
    samples: int  # width / columns
    lines: int  # height / rows
    bands: int
    dtype: str
    envi_data_type: int
    interleave: str
    byte_order: int
    nodata_value: float | None
    stats: dict[str, Any]
    hdr_file: str
    dat_file: str


def write_envi_raster(
    base_path: Path | str,
    raw_bytes: bytes,
    samples: int,
    lines: int,
    bands: int = 1,
    dtype: str = "float32",
    interleave: str = "bsq",
    nodata_value: float | None = None,
    band_names: list[str] | None = None,
    map_info: str | None = None,
    description: str = "Spectra Workbench Synthetic Fixture",
) -> tuple[Path, Path]:
    base = Path(base_path)
    base.parent.mkdir(parents=True, exist_ok=True)
    hdr_path = base.with_suffix(".hdr")
    dat_path = base.with_suffix(".dat")

    envi_code, _, _ = ENVI_DTYPES[dtype]

    # Write binary data
    dat_path.write_bytes(raw_bytes)

    # Write ENVI header text
    hdr_lines = [
        "ENVI",
        f"description = {{{description}}}",
        f"samples = {samples}",
        f"lines   = {lines}",
        f"bands   = {bands}",
        "header offset = 0",
        "file type = ENVI Standard",
        f"data type = {envi_code}",
        f"interleave = {interleave}",
        "byte order = 0",  # Little-endian
    ]
    if nodata_value is not None:
        hdr_lines.append(f"data ignore value = {nodata_value}")
    if band_names:
        names_str = ", ".join(band_names)
        hdr_lines.append(f"band names = {{{names_str}}}")
    if map_info:
        hdr_lines.append(f"map info = {{{map_info}}}")

    hdr_content = "\n".join(hdr_lines) + "\n"
    hdr_path.write_text(hdr_content, encoding="utf-8")

    return hdr_path, dat_path


def read_envi_raster(hdr_path: Path | str, dat_path: Path | str | None = None) -> dict[str, Any]:
    hdr_p = Path(hdr_path)
    dat_p = Path(dat_path) if dat_path else hdr_p.with_suffix(".dat")
    if not dat_p.exists():
        dat_p = hdr_p.with_suffix(".img")

    # Parse HDR
    hdr_info: dict[str, Any] = {}
    lines = hdr_p.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            hdr_info[k.strip().lower()] = v.strip().strip("{}")

    samples = int(hdr_info.get("samples", 0))
    n_lines = int(hdr_info.get("lines", 0))
    bands = int(hdr_info.get("bands", 1))
    envi_code = int(hdr_info.get("data type", 4))

    # Find matching struct format
    fmt_char = "f"
    elem_size = 4
    dtype_name = "float32"
    for name, (code, ch, sz) in ENVI_DTYPES.items():
        if code == envi_code:
            fmt_char = ch
            elem_size = sz
            dtype_name = name
            break

    total_elems = samples * n_lines * bands
    raw_data = dat_p.read_bytes()
    expected_bytes = total_elems * elem_size
    if len(raw_data) < expected_bytes:
        raise ValueError(f"Dat file {dat_p} size {len(raw_data)} < expected {expected_bytes}")

    values = struct.unpack(f"<{total_elems}{fmt_char}", raw_data[:expected_bytes])

    return {
        "header": hdr_info,
        "samples": samples,
        "lines": n_lines,
        "bands": bands,
        "dtype": dtype_name,
        "values": values,
    }


def create_ramp_sample(
    samples: int = 16, lines: int = 16, min_val: float = 0.0, max_val: float = 255.0
) -> tuple[bytes, dict[str, Any]]:
    """A01-1 单波段水平渐变 ramp (Float32)。"""
    vals: list[float] = []
    step = (max_val - min_val) / max(1, samples - 1)
    for _r in range(lines):
        for c in range(samples):
            vals.append(min_val + c * step)
    buf = struct.pack(f"<{len(vals)}f", *vals)
    stats = {
        "min": min(vals),
        "max": max(vals),
        "mean": sum(vals) / len(vals),
        "first_row": vals[:samples],
    }
    return buf, stats


def create_constant_sample(
    samples: int = 8, lines: int = 8, val: float = 42.0
) -> tuple[bytes, dict[str, Any]]:
    """A01-2 常量栅格 (Float32)。"""
    vals = [val] * (samples * lines)
    buf = struct.pack(f"<{len(vals)}f", *vals)
    stats = {
        "min": val,
        "max": val,
        "mean": val,
        "unique_values": [val],
    }
    return buf, stats


def create_multiband_sample(
    samples: int = 8, lines: int = 8, bands: int = 4
) -> tuple[bytes, dict[str, Any]]:
    """A01-3 多波段已知值栅格 (UInt16)，4 波段 (模拟 Red, Green, Blue, NIR)。"""
    vals: list[int] = []
    band_stats: list[dict[str, Any]] = []
    for b in range(bands):
        b_vals: list[int] = []
        base_val = (b + 1) * 1000
        for r in range(lines):
            for c in range(samples):
                v = base_val + r * 10 + c
                b_vals.append(v)
        vals.extend(b_vals)
        band_stats.append(
            {
                "band_idx": b + 1,
                "min": min(b_vals),
                "max": max(b_vals),
                "first_pixel": b_vals[0],
                "last_pixel": b_vals[-1],
            }
        )
    buf = struct.pack(f"<{len(vals)}H", *vals)
    stats = {
        "bands": bands,
        "per_band": band_stats,
    }
    return buf, stats


def create_nodata_sample(
    samples: int = 8, lines: int = 8, nodata: float = -9999.0
) -> tuple[bytes, dict[str, Any]]:
    """A01-4 包含显式 NoData 像元与合法 0 值的栅格 (Float32)。验证 NoData、mask、合法 0 值三者不混为一谈。"""
    vals: list[float] = []
    valid_count = 0
    zero_count = 0
    nodata_count = 0
    for r in range(lines):
        for c in range(samples):
            if r == 0:  # 第一行为 NoData
                v = nodata
                nodata_count += 1
            elif r == 1 and c % 2 == 0:  # 第二行偶数列为合法 0 值
                v = 0.0
                zero_count += 1
                valid_count += 1
            else:
                v = float(r * 10 + c)
                valid_count += 1
            vals.append(v)
    buf = struct.pack(f"<{len(vals)}f", *vals)
    stats = {
        "nodata_value": nodata,
        "nodata_pixels": nodata_count,
        "zero_pixels": zero_count,
        "valid_pixels": valid_count,
        "total_pixels": samples * lines,
    }
    return buf, stats


def generate_all_fixtures(output_dir: Path | str) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": "1.0",
        "description": "Spectra Workbench A01 验收样例库",
        "fixtures": {},
    }

    # 1. Ramp
    ramp_buf, ramp_stats = create_ramp_sample(16, 16, 0.0, 255.0)
    hdr1, dat1 = write_envi_raster(
        out_dir / "a01_ramp_16x16",
        ramp_buf,
        16,
        16,
        1,
        "float32",
        map_info="UTM, 1, 1, 400000.0, 4400000.0, 30.0, 30.0, 50, North, WGS-84",
        band_names=["Ramp_Float32"],
        description="A01-1 单波段 ramp 渐变 (16x16 Float32)",
    )
    manifest["fixtures"]["ramp_16x16"] = asdict(
        SyntheticSampleMeta(
            name="ramp_16x16",
            description="A01-1 单波段水平线性渐变 (16x16 Float32)",
            samples=16,
            lines=16,
            bands=1,
            dtype="float32",
            envi_data_type=4,
            interleave="bsq",
            byte_order=0,
            nodata_value=None,
            stats=ramp_stats,
            hdr_file=hdr1.name,
            dat_file=dat1.name,
        )
    )

    # 2. Constant
    const_buf, const_stats = create_constant_sample(8, 8, 42.0)
    hdr2, dat2 = write_envi_raster(
        out_dir / "a01_constant_8x8",
        const_buf,
        8,
        8,
        1,
        "float32",
        band_names=["Constant_42"],
        description="A01-2 常量像元 (8x8 Float32, 值为 42.0)",
    )
    manifest["fixtures"]["constant_8x8"] = asdict(
        SyntheticSampleMeta(
            name="constant_8x8",
            description="A01-2 常量栅格 (8x8 Float32)",
            samples=8,
            lines=8,
            bands=1,
            dtype="float32",
            envi_data_type=4,
            interleave="bsq",
            byte_order=0,
            nodata_value=None,
            stats=const_stats,
            hdr_file=hdr2.name,
            dat_file=dat2.name,
        )
    )

    # 3. Multiband
    mb_buf, mb_stats = create_multiband_sample(8, 8, 4)
    hdr3, dat3 = write_envi_raster(
        out_dir / "a01_multiband_4b_8x8",
        mb_buf,
        8,
        8,
        4,
        "uint16",
        band_names=["Blue", "Green", "Red", "NIR"],
        description="A01-3 多波段已知值栅格 (8x8, 4 波段 UInt16)",
    )
    manifest["fixtures"]["multiband_4b_8x8"] = asdict(
        SyntheticSampleMeta(
            name="multiband_4b_8x8",
            description="A01-3 4波段已知值栅格 (8x8 UInt16)",
            samples=8,
            lines=8,
            bands=4,
            dtype="uint16",
            envi_data_type=12,
            interleave="bsq",
            byte_order=0,
            nodata_value=None,
            stats=mb_stats,
            hdr_file=hdr3.name,
            dat_file=dat3.name,
        )
    )

    # 4. NoData
    nd_buf, nd_stats = create_nodata_sample(8, 8, -9999.0)
    hdr4, dat4 = write_envi_raster(
        out_dir / "a01_nodata_masked_8x8",
        nd_buf,
        8,
        8,
        1,
        "float32",
        nodata_value=-9999.0,
        band_names=["NoData_and_Zero"],
        description="A01-4 包含 NoData 与合法 0 值的栅格 (8x8 Float32)",
    )
    manifest["fixtures"]["nodata_masked_8x8"] = asdict(
        SyntheticSampleMeta(
            name="nodata_masked_8x8",
            description="A01-4 包含 NoData 与合法 0 值的栅格 (8x8 Float32)",
            samples=8,
            lines=8,
            bands=1,
            dtype="float32",
            envi_data_type=4,
            interleave="bsq",
            byte_order=0,
            nodata_value=-9999.0,
            stats=nd_stats,
            hdr_file=hdr4.name,
            dat_file=dat4.name,
        )
    )

    # Write manifest.json
    manifest_file = out_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
