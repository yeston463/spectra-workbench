"""P4-04 单元测试：受限表达式的解析错误、除零、整数溢出与 mask 语义。

这些用例不启动 worker、不导入 osgeo，直接验证纯 numpy 求值器，
保证「安全边界」与「数据语义」都可以在 GUI 侧被快速回归。
"""

from __future__ import annotations

import numpy as np
import pytest

from spectra_workbench.providers.gdal_provider.expression_ops import (
    ExpressionError,
    band_names_for_inputs,
    evaluate_expression,
    safe_compile,
)

pytestmark = [pytest.mark.unit]


# ------------------------------------------------------------------ 解析错误


@pytest.mark.parametrize(
    "expr",
    [
        "A +",                       # 语法错误
        "C + 1",                     # 未声明的波段名
        "A.__class__",               # 属性访问
        "A[0]",                      # 下标
        "(lambda x: x)(A)",          # lambda / 任意调用
        "A > 1",                     # 比较
        "A and B",                   # 布尔运算
        "A + True",                  # bool 常量（int 子类，必须拒绝）
        "'A'",                       # 字符串常量
        "__import__('os')",          # 任意函数调用 + 字符串
        "sqrt(x=A)",                 # 关键字参数
        "min(A)",                    # 白名单函数参数个数错误
        "abs(A, B)",                 # 白名单函数参数个数错误
        "A @ B",                     # 未白名单的矩阵乘运算符
        "A if B else 0",             # 条件表达式
        "[A]",                       # 列表字面量
    ],
)
def test_safe_compile_rejects_out_of_whitelist(expr: str) -> None:
    with pytest.raises(ExpressionError):
        safe_compile(expr, ["A", "B"])


@pytest.mark.parametrize(
    "expr",
    [
        "(A-B)/(A+B)",
        "max(A, B) - min(A, B)",
        "sqrt(abs(A)) + log10(B) + exp(A) + log(B)",
        "a + b",                     # 波段名大小写不敏感
        "A ** 2 + -3.5",
    ],
)
def test_safe_compile_accepts_whitelist(expr: str) -> None:
    assert safe_compile(expr, ["A", "B"]) is not None


def test_band_names_follow_input_order() -> None:
    assert band_names_for_inputs(3) == ["A", "B", "C"]
    names = band_names_for_inputs(27)
    assert names[25] == "Z" and names[26] == "A1"
    assert len(set(names)) == 27


# ------------------------------------------------------------------ 求值语义


def _arrays() -> tuple[dict[str, np.ndarray], dict[str, float | None]]:
    return (
        {"A": np.array([1.0, 2.0, 3.0], dtype=np.float32),
         "B": np.array([1.0, 0.0, 0.0], dtype=np.float32)},
        {"A": None, "B": None},
    )


def test_division_by_zero_becomes_output_nodata_with_count() -> None:
    bands, nd = _arrays()
    result, info = evaluate_expression("A/B", bands, src_nodata=nd, out_nodata=-9999.0)
    assert float(result[0]) == pytest.approx(1.0)
    assert float(result[1]) == -9999.0 and float(result[2]) == -9999.0
    assert info["n_math_invalid"] == 2        # 2/0 与 3/0 均转 NoData
    assert info["n_output_invalid"] == 2
    assert info["dtype"] == "float32"


def test_zero_over_zero_is_nan_then_nodata() -> None:
    bands = {"A": np.array([0.0, 1.0], dtype=np.float32),
             "B": np.array([0.0, 0.0], dtype=np.float32)}
    result, info = evaluate_expression(
        "A/B", bands, src_nodata={"A": None, "B": None}, out_nodata=-9999.0
    )
    assert np.all(result == -9999.0)
    assert info["n_math_invalid"] == 2


def test_int16_overflow_is_prevented_by_float32_promotion() -> None:
    """实测：int16 30000+30000 回绕为 -5536；平台先提升 float32，得 60000。"""
    a = np.array([30000, 30000], dtype=np.int16)
    with np.errstate(over="ignore"):
        wrapped = a + a
    assert int(wrapped[0]) != 60000, "前置条件：numpy int16 加法确实回绕"

    result, info = evaluate_expression(
        "A+A", {"A": a}, src_nodata={"A": None}, out_nodata=-9999.0
    )
    assert result.tolist() == [60000.0, 60000.0]
    assert info["n_math_invalid"] == 0


def test_integer_output_overflow_is_rejected_not_wrapped() -> None:
    a = np.array([30000], dtype=np.int16)
    with pytest.raises(ExpressionError, match="超出输出类型"):
        evaluate_expression(
            "A+A", {"A": a}, src_nodata={"A": None},
            out_nodata=-9999, out_dtype=np.int16,
        )


def test_integer_output_decimal_is_rejected_not_truncated() -> None:
    a = np.array([1.0], dtype=np.float32)
    with pytest.raises(ExpressionError, match="会静默截断"):
        evaluate_expression(
            "A/2", {"A": a}, src_nodata={"A": None},
            out_nodata=-9999, out_dtype=np.int16,
        )


def test_input_nodata_mask_propagates_to_output() -> None:
    bands = {
        "A": np.array([-9999.0, 2.0, 4.0], dtype=np.float32),
        "B": np.array([1.0, 1.0, 1.0], dtype=np.float32),
    }
    result, info = evaluate_expression(
        "A+B", bands, src_nodata={"A": -9999.0, "B": None}, out_nodata=-9999.0
    )
    assert float(result[0]) == -9999.0
    assert float(result[1]) == pytest.approx(3.0)
    assert info["n_input_invalid"] == 1
    assert info["n_math_invalid"] == 0


def test_non_finite_input_is_invalid() -> None:
    bands = {"A": np.array([np.nan, 2.0], dtype=np.float32)}
    result, info = evaluate_expression(
        "A*2", bands, src_nodata={"A": None}, out_nodata=-9999.0
    )
    assert float(result[0]) == -9999.0 and float(result[1]) == pytest.approx(4.0)
    assert info["n_input_invalid"] == 1


def test_valid_value_equal_to_out_nodata_is_rejected() -> None:
    """有效区结果恰好等于 NoData 会造成「缺口变有效值」的歧义，必须拒绝。"""
    bands = {"A": np.array([1.0, 2.0], dtype=np.float32)}
    with pytest.raises(ExpressionError, match="等于输出 NoData"):
        evaluate_expression(
            "A*0", bands, src_nodata={"A": None}, out_nodata=0.0
        )


def test_case_insensitive_band_names_evaluate() -> None:
    bands = {"A": np.array([2.0], dtype=np.float32),
             "B": np.array([3.0], dtype=np.float32)}
    result, _ = evaluate_expression(
        "a+b", bands, src_nodata={"A": None, "B": None}, out_nodata=-9999.0
    )
    assert float(result[0]) == pytest.approx(5.0)


def test_constant_expression_broadcasts_to_band_shape() -> None:
    bands = {"A": np.ones((2, 3), dtype=np.float32)}
    result, info = evaluate_expression(
        "2+3", bands, src_nodata={"A": None}, out_nodata=-9999.0
    )
    assert result.shape == (2, 3)
    assert np.all(result == 5.0)
    assert info["n_valid"] == 6
