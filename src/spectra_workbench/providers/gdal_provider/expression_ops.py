"""P4-04：栅格表达式计算（安全解析 + numpy 求值，**不依赖 osgeo**）。

原验收标准（docs/plan/02_里程碑与验收.md）：
「解析错误、除零、整数溢出、mask、抽样/全量统计模式有证据」

**实测结论（GDAL 3.13.3 + numpy，本模块实现依据，见 work/_probe_p404.py）**：
- float 除零：产出 NaN（0/0）与 Infinity（x/0），不抛异常（errstate 控制下）
  → 平台语义：数学上无效的像元**转为输出 NoData**（进无效区），并有计数证据；
- int16 加法**回绕**（30000+30000 = -5536）；int/int 除法自动提升 float64；
  **float32 提升后相加不回绕**（60000）→ 表达式计算统一先把波段提升为
  float32，从源头避免整数溢出；若输出 dtype 显式指定为整型，还会在
  :func:`evaluate_expression` 里做范围/取整校验并拒绝，防止二次回绕；
- 无效像元参与计算用 mask 隔离（无效像元保持输出 NoData）。

**安全模型（关键）**：表达式**不用 eval**。用 ast 解析为语法树后按白名单
遍历执行——只允许数值常量、声明的波段名、白名单运算符与白名单函数；
属性访问、下标、lambda、推导式、比较、布尔运算、关键字参数、任何
非常量调用一律在求值前拒绝（ExpressionError）。本模块是纯 numpy，
GUI 侧也可安全导入（不违反「GUI 不 import 原生地理依赖」约束）。
"""

from __future__ import annotations

import ast
from typing import Any

import numpy as np

__all__ = [
    "ExpressionError",
    "safe_compile",
    "band_names_for_inputs",
    "evaluate_expression",
]

_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": np.abs,
    "min": np.minimum,   # 双参语义：逐像元 min(A,B)
    "max": np.maximum,   # 逐像元 max(A,B)
    "sqrt": np.sqrt,
    "exp": np.exp,
    "log": np.log,
    "log10": np.log10,
}

#: 白名单函数的参数个数（逐像元 min/max 是二元，其余一元）。
_FUNC_ARITY: dict[str, int] = {
    "abs": 1, "min": 2, "max": 2, "sqrt": 1, "exp": 1, "log": 1, "log10": 1,
}

_ALLOWED_BINOPS = {
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
}
_ALLOWED_UNARYOPS = {ast.USub, ast.UAdd}

_MAX_EXPR_LEN = 512


class ExpressionError(Exception):
    """表达式解析、白名单校验或输出语义失败（提交前/求值前即拒绝）。"""


def band_names_for_inputs(count: int) -> list[str]:
    """按输入顺序映射波段名：第 1 个输入 = A，第 2 个 = B……（gdal_calc 语义）。"""
    names: list[str] = []
    for i in range(count):
        # A, B, ..., Z, A1, B1（超过 26 个输入时加序号）
        base = chr(ord("A") + i % 26)
        names.append(base if i < 26 else f"{base}{i // 26}")
    return names


def safe_compile(expr: str, allowed_names: list[str]) -> ast.expr:
    """把表达式解析为语法树并做白名单校验（不 eval，直接拒绝越权节点）。

    波段名大小写不敏感（``a`` 等价于 ``A``）；其余语法严格受限。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ExpressionError("表达式不能为空")
    if len(expr) > _MAX_EXPR_LEN:
        raise ExpressionError(f"表达式过长（>{_MAX_EXPR_LEN} 字符）")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误：{exc.msg}（位置 {exc.offset}）") from exc

    names = {n.upper() for n in allowed_names}
    funcs = set(_ALLOWED_FUNCS)

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            walk(node.body)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_BINOPS:
                raise ExpressionError(f"不支持的运算符：{type(node.op).__name__}")
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _ALLOWED_UNARYOPS:
                raise ExpressionError(f"不支持的一元运算符：{type(node.op).__name__}")
            walk(node.operand)
        elif isinstance(node, ast.Name):
            if node.id.upper() not in names:
                raise ExpressionError(
                    f"未知名称 {node.id!r}；可用波段名：{', '.join(allowed_names)}"
                )
        elif isinstance(node, ast.Constant):
            # bool 是 int 的子类，必须显式拒绝；字符串/复数/None 同样拒绝。
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExpressionError(
                    f"只允许数值常量，拒绝 {type(node.value).__name__} 常量"
                )
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in funcs:
                got = getattr(node.func, "id", type(node.func).__name__)
                raise ExpressionError(
                    f"只允许白名单函数 {sorted(funcs)}，拒绝 {got!r}"
                )
            if node.keywords:
                raise ExpressionError(f"函数 {node.func.id!r} 不接受关键字参数")
            expected = _FUNC_ARITY[node.func.id]
            if len(node.args) != expected:
                raise ExpressionError(
                    f"函数 {node.func.id!r} 需要 {expected} 个参数，实际 {len(node.args)} 个"
                )
            for arg in node.args:
                walk(arg)
        else:
            raise ExpressionError(
                f"表达式中不允许出现 {type(node).__name__} 节点"
                "（禁止属性访问/下标/lambda/比较/布尔运算等）"
            )

    walk(tree)
    return tree.body


def evaluate_expression(
    expr: str,
    bands: dict[str, np.ndarray],
    *,
    src_nodata: dict[str, float | None],
    out_nodata: float,
    out_dtype: Any = np.float32,
) -> tuple[np.ndarray, dict[str, Any]]:
    """编译并求值表达式，返回 (结果数组, 元信息)。

    求值语义：
    - 波段先提升 float32（防整数回绕——实测 int16 加法会回绕）；
    - 每个波段的无效像元（== 各自 src_nodata 或非有限）先排除出计算，
      计算后**回填**输出 NoData，保持无效区传播（mask 语义）；
    - 除零等数学无效（NaN/Inf，实测 float 除零产 NaN/Inf）转为输出 NoData，
      并计数（n_math_invalid），不静默吞掉；
    - 有效区结果若恰好等于 out_nodata，或整型输出会溢出/截断小数，
      明确拒绝（不产出无法区分有效值与 NoData 的成果）。
    """
    arrays_upper = {k.upper(): v for k, v in bands.items()}
    allowed = list(arrays_upper.keys())
    node = safe_compile(expr, allowed)

    arrays: dict[str, np.ndarray] = {}
    invalid_masks: dict[str, np.ndarray] = {}
    for name, arr in arrays_upper.items():
        f = np.asarray(arr).astype(np.float32)
        nd = src_nodata.get(name, src_nodata.get(name.lower()))
        mask = ~np.isfinite(f)
        if nd is not None:
            mask |= f == np.float32(nd)
        arrays[name] = f
        invalid_masks[name] = mask

    def eval_node(n: ast.AST) -> Any:
        if isinstance(n, ast.BinOp):
            left = eval_node(n.left)
            right = eval_node(n.right)
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                if isinstance(n.op, ast.Add):
                    return left + right
                if isinstance(n.op, ast.Sub):
                    return left - right
                if isinstance(n.op, ast.Mult):
                    return left * right
                if isinstance(n.op, ast.Div):
                    return left / right
                if isinstance(n.op, ast.FloorDiv):
                    return left // right
                if isinstance(n.op, ast.Mod):
                    return left % right
                if isinstance(n.op, ast.Pow):
                    return left**right
            raise ExpressionError("不支持的运算符")
        if isinstance(n, ast.UnaryOp):
            v = eval_node(n.operand)
            return -v if isinstance(n.op, ast.USub) else +v
        if isinstance(n, ast.Name):
            return arrays[n.id.upper()]
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return np.float32(n.value)
        if isinstance(n, ast.Call):
            func = n.func
            if not isinstance(func, ast.Name):
                raise ExpressionError("只允许调用白名单函数")
            if func.id not in _ALLOWED_FUNCS:
                raise ExpressionError(f"不允许调用函数 {func.id!r}")
            fn = _ALLOWED_FUNCS[func.id]
            args = [eval_node(a) for a in n.args]
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                return fn(*args)
        raise ExpressionError(f"无法求值的节点 {type(n).__name__}")

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        raw = np.asarray(eval_node(node), dtype=np.float32)
    # 纯常量表达式（如 "1+1"）返回 0 维标量；按第一个波段形状广播。
    if raw.ndim == 0 and arrays:
        raw = np.full(next(iter(arrays.values())).shape, float(raw), dtype=np.float32)

    # 无效区传播：任一输入在该像元无效 → 输出无效
    combined_invalid = np.zeros(raw.shape, dtype=bool)
    for m in invalid_masks.values():
        combined_invalid |= m

    # 数学无效（NaN/Inf，除零/溢出等）→ 输出 NoData 并计数
    math_invalid = ~np.isfinite(raw) & ~combined_invalid
    invalid = combined_invalid | math_invalid
    valid_values = raw[~invalid]

    conflict = int(np.count_nonzero(valid_values == np.float32(out_nodata)))
    if conflict:
        raise ExpressionError(
            f"有效区有 {conflict} 个像元的计算结果等于输出 NoData {out_nodata}，"
            "会产生「有效值被当成缺口」，请调整 dstNodata 或表达式后重试"
        )

    dtype = np.dtype(out_dtype if out_dtype is not None else np.float32)
    if np.issubdtype(dtype, np.integer):
        info_i = np.iinfo(dtype)
        finite = valid_values[np.isfinite(valid_values)]
        if finite.size and (finite.min() < info_i.min or finite.max() > info_i.max):
            raise ExpressionError(
                f"结果范围 [{finite.min():.4g}, {finite.max():.4g}] 超出输出类型 "
                f"{dtype.name} 的 [{info_i.min}, {info_i.max}]，会整数回绕；"
                "请改用 Float32 输出或调整表达式"
            )
        if finite.size and np.any(finite != np.floor(finite)):
            raise ExpressionError(
                f"结果含小数，整型输出 {dtype.name} 会静默截断；请改用 Float32 输出"
            )

    result = np.where(invalid, np.float32(out_nodata), raw).astype(dtype)
    info = {
        "expression": expr,
        "shape": list(raw.shape),
        "band_names": allowed,
        "n_input_invalid": int(combined_invalid.sum()),
        "n_math_invalid": int(math_invalid.sum()),
        "n_output_invalid": int(invalid.sum()),
        "n_valid": int(valid_values.size),
        "out_nodata": float(out_nodata),
        "dtype": dtype.name,
    }
    return result, info
