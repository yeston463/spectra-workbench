"""GDAL 栅格处理参数的**单一契约源**（GUI 与 Worker 共用）。

为什么需要它
------------
第 25 轮复核发现三处「GUI 发的参数 Worker 不认」的静默失效：

1. GUI 写 ``parameters['resampling']='bilinear'``，Worker 只读 ``resample_alg``，
   于是双线性被静默执行成最近邻；
2. GUI 下拉框提供 5 种算法，Worker 的映射表只有 4 种，且名字对不上
   （GUI 的 ``near``、``lanczos`` 都不在表里）——选中后静默回退到最近邻；
3. ``dstNodata=0`` 被 ``or`` 短路丢弃，合法的零值参数丢失。

根因是**两处各维护一份参数名与取值集合**。本模块把契约收敛到一处：
GUI 提交前与 Worker 执行前都调用同一个 :func:`normalize_raster_params`，
从而做到「拼错即报错、未知算法不回退、别名冲突不择一」。

本模块刻意**不导入 osgeo**（只含纯 Python 数据与校验），
因此 GUI 进程可以安全导入，不违反「GUI 不 import 原生地理依赖」的硬约束。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "ParameterError",
    "RESAMPLE_ALGORITHMS",
    "RESAMPLE_ALIASES",
    "canonical_param_key",
    "normalize_raster_params",
    "resample_algorithm_label",
]


class ParameterError(ValueError):
    """参数非法（结构、取值或组合冲突）。

    Worker 侧转换为 ``invalid_parameters`` 错误；GUI 侧转换为提示文案。
    """


# --------------------------------------------------------------------------
# 重采样算法
# --------------------------------------------------------------------------
#: 支持的重采样算法（canonical 名）。Worker 会把名字映射到 GDAL 的 GRA_* 常量。
RESAMPLE_ALGORITHMS: tuple[str, ...] = (
    "nearest",
    "bilinear",
    "cubic",
    "cubic_spline",
    "lanczos",
    "average",
    "mode",
    "max",
    "min",
    "median",
    "q1",
    "q3",
)

#: 用户可写的小写别名 -> canonical 名。
RESAMPLE_ALIASES: dict[str, str] = {
    "near": "nearest",
    "neareast_neighbour": "nearest",
    "nearest_neighbour": "nearest",
    "nearestneighbor": "nearest",
    "bil": "bilinear",
    "cubic_spline": "cubic_spline",
    "cubicspline": "cubic_spline",
    "avg": "average",
    "med": "median",
    "quartile_1": "q1",
    "quartile_3": "q3",
}

#: GUI 下拉框展示用（canonical 名, 显示文本）。
#: **顺序即 GUI 默认值**：第一项是默认重采样算法。保持 bilinear 在首位，
#: 与改造前的 GUI 行为一致（本轮只修参数一致性，不顺带改默认行为）。
RESAMPLE_ALGORITHM_CHOICES: tuple[tuple[str, str], ...] = (
    ("bilinear", "双线性插值 (bilinear)"),
    ("nearest", "最近邻 (nearest)"),
    ("cubic", "三次卷积 (cubic)"),
    ("cubic_spline", "三次样条 (cubic_spline)"),
    ("lanczos", "Lanczos (lanczos)"),
    ("average", "平均 (average)"),
    ("mode", "众数 (mode)"),
    ("max", "最大值 (max)"),
    ("min", "最小值 (min)"),
    ("median", "中位数 (median)"),
    ("q1", "第一四分位 (q1)"),
    ("q3", "第三四分位 (q3)"),
)


def resample_algorithm_label(name: str) -> str:
    """返回算法名的中文显示文本。"""
    for canonical, label in RESAMPLE_ALGORITHM_CHOICES:
        if canonical == name:
            return label
    return name


def _normalize_resample_name(raw: Any) -> str:
    """把用户填写的算法名归一化为 canonical 名；未知则报错（绝不回退）。"""
    if raw is None:
        return "nearest"
    key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if key in RESAMPLE_ALGORITHMS:
        return key
    if key in RESAMPLE_ALIASES:
        return RESAMPLE_ALIASES[key]
    raise ParameterError(
        f"未知的重采样算法 {raw!r}；可用算法：{', '.join(RESAMPLE_ALGORITHMS)}"
    )


# --------------------------------------------------------------------------
# 参数别名表：canonical key -> 可接受的别名（含 CLI 风格与 snake_case）
# --------------------------------------------------------------------------
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "resampleAlg": ("resample_alg", "resampling", "resample", "resample_method"),
    "srcWin": ("src_win", "source_window", "window", "pixel_window"),
    "projWin": ("proj_win", "projection_window", "geo_window"),
    "outputBounds": ("output_bounds", "bounds", "extent"),
    "xRes": ("x_res", "x_resolution", "xresolution"),
    "yRes": ("y_res", "y_resolution", "yresolution"),
    "outputType": ("output_type", "dtype", "data_type"),
    "noData": ("no_data", "nodata", "src_nodata"),
    "dstNodata": ("dst_nodata", "dstnodata", "dst_no_data"),
    "dstSRS": ("dst_srs", "dst_crs", "dstcrs", "target_crs"),
    "srcSRS": ("src_srs", "src_crs", "srccrs", "source_crs"),
    "targetAlignedPixels": ("target_aligned_pixels", "tap", "align_pixels"),
    "width": ("out_width", "outwidth", "output_width"),
    "height": ("out_height", "outheight", "output_height"),
}

#: 允许出现在 parameters 里、但不属于上述栅格几何参数的非几何键。
_OTHER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "format",
        "overwrite",
        "creation_options",
        "driver",
        "band",
        "bands",
        "select",
        "scale",
        "offset",
        "metadata",
        "options",
    }
)

#: canonical key -> 别名（含 canonical 自身）
_ALL_ALIASES: dict[str, tuple[str, ...]] = {
    canonical: (canonical, *aliases) for canonical, aliases in _PARAM_ALIASES.items()
}

#: 反查表：别名 -> canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _names in _ALL_ALIASES.items():
    for _name in _names:
        _ALIAS_TO_CANONICAL[_name] = _canonical


def canonical_param_key(key: str) -> str | None:
    """返回某个参数名的 canonical 名；不是栅格几何参数时返回 None。"""
    return _ALIAS_TO_CANONICAL.get(key)


# --------------------------------------------------------------------------
# 取值解析
# --------------------------------------------------------------------------
def _is_int_like(value: Any) -> bool:
    """是否是整数（含整型浮点），且不接受 bool。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False


def _require_int_list(value: Any, name: str, expected: int) -> list[int]:
    """解析定长整数列表。**小数必须报错，不得静默截断。**"""
    if not isinstance(value, (list, tuple)):
        raise ParameterError(f"参数 {name} 必须是长度 {expected} 的整数列表")
    if len(value) != expected:
        raise ParameterError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    out: list[int] = []
    for v in value:
        if not _is_int_like(v):
            raise ParameterError(
                f"参数 {name} 的元素必须是整数（不接受小数截断），实际含 {v!r}"
            )
        out.append(int(v))
    return out


def _require_float_list(value: Any, name: str, expected: int) -> list[float]:
    """解析定长浮点列表（要求有限值）。"""
    if not isinstance(value, (list, tuple)):
        raise ParameterError(f"参数 {name} 必须是长度 {expected} 的数值列表")
    if len(value) != expected:
        raise ParameterError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    out: list[float] = []
    for v in value:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ParameterError(f"参数 {name} 含非数值元素：{v!r}") from exc
        if not math.isfinite(f):
            raise ParameterError(f"参数 {name} 含非有限值：{v!r}")
        out.append(f)
    return out


def _require_positive_float(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(f"参数 {name} 必须是数值，实际 {value!r}") from exc
    if not math.isfinite(f):
        raise ParameterError(f"参数 {name} 必须是有限值，实际 {value!r}")
    if f <= 0:
        raise ParameterError(f"参数 {name} 必须为正数，实际 {f}")
    return f


def _require_positive_int(value: Any, name: str) -> int:
    if not _is_int_like(value):
        raise ParameterError(f"参数 {name} 必须是整数（不接受小数截断），实际 {value!r}")
    iv = int(value)
    if iv <= 0:
        raise ParameterError(f"参数 {name} 必须为正数，实际 {iv}")
    return iv


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def normalize_raster_params(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化并校验栅格处理参数。

    返回 canonical 名 -> 解析后的值（只含**实际提供**的参数）。
    任何非法取值、未知算法、别名冲突都抛 :class:`ParameterError`。

    关键约定：
    - **零值是合法值**：按「是否提供 / 是否为 None」判断，绝不用 truthiness；
    - **未知算法不���退**：直接报错；
    - **小数不截断**：整数参数遇到小数报错；
    - **别名冲突不择一**：同一 canonical 的多个别名给出不同值时报错。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ParameterError(f"parameters 必须是映射类型，实际 {type(raw).__name__}")

    # 1) 收集 canonical -> (别名, 值)，检测冲突
    collected: dict[str, tuple[str, Any]] = {}
    for key, value in raw.items():
        canonical = _ALIAS_TO_CANONICAL.get(key)
        if canonical is None:
            if key in _OTHER_ALLOWED_KEYS:
                continue
            raise ParameterError(
                f"未知参数 {key!r}；可用参数名："
                f"{', '.join(sorted(_ALIAS_TO_CANONICAL))}（另有 {', '.join(sorted(_OTHER_ALLOWED_KEYS))} 等非几何参数）"
            )
        if value is None:
            continue
        if canonical in collected:
            prev_name, prev_value = collected[canonical]
            if prev_value != value:
                raise ParameterError(
                    f"参数冲突：{prev_name!r} 与 {key!r} 同指 {canonical} 但取值不同"
                    f"（{prev_value!r} vs {value!r}）"
                )
            continue
        collected[canonical] = (key, value)

    out: dict[str, Any] = {}

    # 2) 重采样算法
    if "resampleAlg" in collected:
        _, value = collected["resampleAlg"]
        out["resampleAlg"] = _normalize_resample_name(value)

    # 3) 源窗口
    if "srcWin" in collected:
        _, value = collected["srcWin"]
        src_win = _require_int_list(value, "srcWin", 4)
        if src_win[2] <= 0 or src_win[3] <= 0:
            raise ParameterError(f"srcWin 的宽高必须为正数，实际 {src_win}")
        out["srcWin"] = src_win

    # 4) 源地理窗口
    if "projWin" in collected:
        _, value = collected["projWin"]
        out["projWin"] = _require_float_list(value, "projWin", 4)

    # 5) 目标范围
    if "outputBounds" in collected:
        _, value = collected["outputBounds"]
        out["outputBounds"] = _require_float_list(value, "outputBounds", 4)

    # 6) 输出像元尺寸
    if "xRes" in collected:
        _, value = collected["xRes"]
        out["xRes"] = _require_positive_float(value, "xRes")
    if "yRes" in collected:
        _, value = collected["yRes"]
        out["yRes"] = _require_positive_float(value, "yRes")

    # 7) 输出尺寸
    if "width" in collected:
        _, value = collected["width"]
        out["width"] = _require_positive_int(value, "width")
    if "height" in collected:
        _, value = collected["height"]
        out["height"] = _require_positive_int(value, "height")

    # 8) 输出数据类型（名字 -> 规范名，映射到 GDAL 常量由 worker 负责）
    if "outputType" in collected:
        _, value = collected["outputType"]
        out["outputType"] = _normalize_output_type(value)

    # 9) NoData：零值合法，按是否为 None 判断
    if "noData" in collected:
        _, value = collected["noData"]
        out["noData"] = _require_finite_float(value, "noData")
    if "dstNodata" in collected:
        _, value = collected["dstNodata"]
        out["dstNodata"] = _require_finite_float(value, "dstNodata")

    # 10) CRS
    if "dstSRS" in collected:
        _, value = collected["dstSRS"]
        out["dstSRS"] = str(value).strip()
    if "srcSRS" in collected:
        _, value = collected["srcSRS"]
        out["srcSRS"] = str(value).strip()

    # 11) 输出网格对齐
    if "targetAlignedPixels" in collected:
        _, value = collected["targetAlignedPixels"]
        if not isinstance(value, bool):
            raise ParameterError(f"targetAlignedPixels 必须是布尔值，实际 {value!r}")
        out["targetAlignedPixels"] = value

    return out


def _require_finite_float(value: Any, name: str) -> float:
    """解析有限浮点数（零值合法）。"""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(f"参数 {name} 必须是数值，实际 {value!r}") from exc
    if not math.isfinite(f):
        raise ParameterError(f"参数 {name} 必须是有限值，实际 {value!r}")
    return f


_OUTPUT_TYPE_ALIASES: dict[str, str] = {
    "byte": "Byte",
    "uint8": "Byte",
    "ubyte": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
    "cfloat32": "CFloat32",
    "cfloat64": "CFloat64",
}


def _normalize_output_type(value: Any) -> str:
    """把输出数据类型名归一化为 GDAL 规范名；未知则报错。"""
    key = str(value).strip().lower()
    if key in _OUTPUT_TYPE_ALIASES:
        return _OUTPUT_TYPE_ALIASES[key]
    if key.startswith("gdt_"):
        candidate = key[4:]
        if candidate in set(_OUTPUT_TYPE_ALIASES.values()):
            return candidate
    raise ParameterError(
        f"未知的输出数据类型 {value!r}；可用：{', '.join(sorted(_OUTPUT_TYPE_ALIASES))}"
    )
