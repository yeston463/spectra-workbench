"""P8-01 多维数组：数据树探测与二维切片（GDAL multidim API）。

**仅 worker 进程 import**；GUI 进程不得 import 本模块（AGENTS #1）。

语义（计划 §9.3，验收「合成 netCDF/HDF5 的 time/depth/x/y、单位和切片像元
正确」）：
- `build_multidim_tree` 展开 group/attributes/dimensions/arrays：数组节点含
  dims/shape/unit/scale/offset/nodata/srs_wkt——**原值空间与物理量元数据
  分离记录**（AGENTS #4：不用一个布尔把两件事混起来）；
- `run_multidim_slice` 把非地理维以显式索引钉死后导出**二维切片**（剩余
  两维按数组原序组成 rows/cols），输出像元 = **raw 存储值**（不做
  scale/offset 数学换算）；unit/scale/offset/NoData 写入 sidecar，并作为
  band 元数据（SetUnitType/SetScale/SetOffset）保存在 GTiff 中；
- **不编造地理参考**：array 无 SpatialRef 时输出不带 CRS/GeoTransform，
  sidecar 标 `georeferenced=false`；
- 复数/布尔数组、未知维度、超界索引、钉死数量错误等显式拒绝。

边界
----
- 本运行时 **HDF5 writer 缺失**（`gdal.GetDriverByName('HDF5')` 为空），
  多维容器按 GDAL multidim 统一模型以 netCDF/CF 结构读写验收；
  HDF5 文件读写待运行时补齐驱动后另行验证；
- netCDF **创建**受 P3-02 定档限制：中文/空格路径不可写、且只能以
  「chdir 到目录 + 相对文件名」创建（读取不受限）——测试装置遵守同一约定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["MDimError", "build_multidim_tree", "run_multidim_slice"]


class MDimError(RuntimeError):
    """多维处理错误（探测/切片失败语义化抛出，adapter 转任务错误）。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _guard() -> None:
    try:
        _gdal()
    except Exception as exc:
        raise MDimError(f"GDAL 不可用：多维处理必须在 GDAL worker 运行时内执行（{exc}）") from exc


def _dtype_to_gdt(gdal: Any, np_dtype: Any) -> Any:
    """numpy dtype → GDAL GDT；复数/布尔/对象显式拒绝（不猜驱动能力）。"""
    kind = np_dtype.kind
    if kind == "f":
        return gdal.GDT_Float64 if np_dtype.itemsize == 8 else gdal.GDT_Float32
    if kind == "i":
        return {
            1: gdal.GDT_Int8,
            2: gdal.GDT_Int16,
            4: gdal.GDT_Int32,
            8: gdal.GDT_Int64,
        }[np_dtype.itemsize]
    if kind == "u":
        return {
            1: gdal.GDT_Byte,
            2: gdal.GDT_UInt16,
            4: gdal.GDT_UInt32,
            8: gdal.GDT_UInt64,
        }[np_dtype.itemsize]
    raise MDimError(f"复数/布尔/对象数组不支持二维切片（拒绝）：dtype={np_dtype!r}")


def _attr_value(at: Any, gedtc_string: int) -> Any:
    dt = at.GetDataType()
    if dt.GetClass() == gedtc_string:
        return at.ReadAsString()
    return at.ReadAsDouble()


def _num_attr(attrs: dict[str, Any], *names: str) -> float | None:
    for n in names:
        v = attrs.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _array_info(arr: Any, gedtc_string: int) -> dict[str, Any]:
    """数组节点：dims/shape/unit/scale/offset/nodata/srs 原样记录（无则 None）。"""
    attrs: dict[str, Any] = {}
    for at in arr.GetAttributes():
        attrs[at.GetName()] = _attr_value(at, gedtc_string)
    srs = arr.GetSpatialRef()
    try:
        nodata = (
            float(arr.GetNoDataValueAsDouble()) if arr.GetNoDataValueAsRaw() is not None else None
        )
    except Exception:
        nodata = None
    if nodata is None:
        nodata = _num_attr(attrs, "_FillValue", "missing_value", "nodata")
    scale = arr.GetScale()
    if scale is None:
        scale = _num_attr(attrs, "scale_factor")
    offset = arr.GetOffset()
    if offset is None:
        offset = _num_attr(attrs, "add_offset")
    return {
        "name": arr.GetName(),
        "dims": [d.GetName() for d in arr.GetDimensions()],
        "shape": [int(s) for s in arr.GetShape()],
        "unit": arr.GetUnit()
        or (attrs.get("units") if isinstance(attrs.get("units"), str) else None),
        "scale": float(scale) if scale is not None else None,
        "offset": float(offset) if offset is not None else None,
        "nodata": nodata,
        "srs_wkt": srs.ExportToWkt() if srs is not None else None,
        "attrs": attrs,
    }


def _group_tree(rg: Any, gedtc_string: int) -> dict[str, Any]:
    """递归展开 group：attrs/dims/arrays/子组；坐标数组存在与否如实标注。"""
    array_names = list(rg.GetMDArrayNames())
    return {
        "group": rg.GetName(),
        "attrs": {at.GetName(): _attr_value(at, gedtc_string) for at in rg.GetAttributes()},
        "dims": [
            {
                "name": d.GetName(),
                "size": int(d.GetSize()),
                "has_coordinate_array": d.GetName() in array_names,
            }
            for d in rg.GetDimensions()
        ],
        "arrays": [_array_info(rg.OpenMDArray(n), gedtc_string) for n in array_names],
        "groups": [_group_tree(rg.OpenGroup(gn), gedtc_string) for gn in list(rg.GetGroupNames())],
    }


def build_multidim_tree(path: Path | str) -> dict[str, Any]:
    """多维数据树：group/attributes/dimensions/arrays（GDAL multidim 模型）。"""
    _guard()
    gdal = _gdal()
    gedtc_string = int(gdal.GEDTC_STRING)
    try:
        ds = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER | gdal.OF_READONLY)
    except Exception as exc:
        raise MDimError(f"无法打开多维数据集：{path}（{exc}）") from exc
    if ds is None:
        raise MDimError(f"无法打开多维数据集：{path}")
    try:
        rg = ds.GetRootGroup()
        if rg is None:
            raise MDimError(f"数据集没有多维根组（非多维容器）：{path}")
        return _group_tree(rg, gedtc_string)
    finally:
        del ds


def _view_expr(rank: int, selected: dict[int, int]) -> str:
    items = [str(int(selected[i])) if i in selected else ":" for i in range(rank)]
    return "[" + ",".join(items) + "]"


def run_multidim_slice(
    in_path: Path | str,
    array: str,
    dim_select: dict[str, int],
    staged_path: Path | str,
) -> dict[str, Any]:
    """把非地理维钉死后导出二维切片（GTiff raw 值 + sidecar 元数据）。

    Returns:
        info dict（sidecar 路径、形状、被选维度坐标值、单位、georef 标记）。
    """
    _guard()
    gdal = _gdal()
    gedtc_string = int(gdal.GEDTC_STRING)
    in_path = Path(in_path)
    staged_path = Path(staged_path)
    try:
        ds = gdal.OpenEx(str(in_path), gdal.OF_MULTIDIM_RASTER | gdal.OF_READONLY)
    except Exception as exc:
        raise MDimError(f"无法打开多维数据集：{in_path}（{exc}）") from exc
    if ds is None:
        raise MDimError(f"无法打开多维数据集：{in_path}")
    try:
        rg = ds.GetRootGroup()
        if rg is None:
            raise MDimError(f"数据集没有多维根组：{in_path}")

        candidates = [rg] + [rg.OpenGroup(gn) for gn in list(rg.GetGroupNames())]
        arr = None
        for grp in candidates:
            if array in list(grp.GetMDArrayNames()):
                arr = grp.OpenMDArray(array)
                break
        if arr is None:
            names_all = sorted({n for grp in candidates for n in list(grp.GetMDArrayNames())})
            raise MDimError(f"数组 {array!r} 不存在；可用数组：{', '.join(names_all) or '（无）'}")

        info = _array_info(arr, gedtc_string)
        dims: list[str] = info["dims"]
        shape: list[int] = info["shape"]
        rank = len(dims)
        if rank < 3:
            raise MDimError(
                f"数组 {array!r} 维数 {rank} 不足以切出二维"
                "（需要 ≥3 维：时间/深度/带等 + 两个保留维）"
            )
        unknown = [k for k in dim_select if k not in dims]
        if unknown:
            raise MDimError(f"dimSelect 含未知维度 {unknown}；数组 {array!r} 的维度：{dims}")
        if len(dim_select) != rank - 2:
            raise MDimError(
                f"二维切片需要恰好钉死 {rank - 2} 个维度，"
                f"dimSelect 给出 {len(dim_select)} 个（{sorted(dim_select)}）"
            )
        selected: dict[int, int] = {}
        for k, v in dim_select.items():
            if not isinstance(v, int) or isinstance(v, bool):
                raise MDimError(f"维度索引必须是整数：{k}={v!r}")
            idx = int(v)
            if not 0 <= idx < shape[dims.index(k)]:
                raise MDimError(f"维度 {k!r} 索引 {idx} 越界（size={shape[dims.index(k)]}）")
            selected[dims.index(k)] = idx

        view = arr.GetView(_view_expr(rank, selected))
        nd = view.ReadAsArray()
        if nd is None or getattr(nd, "ndim", 0) != 2:
            raise MDimError(f"切片读取失败：期望二维数组，实得 {getattr(nd, 'ndim', None)} 维")
    finally:
        del ds

    ny, nx = int(nd.shape[0]), int(nd.shape[1])
    gdt = _dtype_to_gdt(_gdal(), nd.dtype)
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(str(staged_path), nx, ny, 1, gdt)
    if out is None:
        raise MDimError(f"GTiff 切片写入失败：{staged_path}")
    try:
        band = out.GetRasterBand(1)
        band.WriteArray(nd)
        if info["nodata"] is not None:
            band.SetNoDataValue(float(info["nodata"]))
        if info["scale"] is not None:
            band.SetScale(float(info["scale"]))
        if info["offset"] is not None:
            band.SetOffset(float(info["offset"]))
        if info["unit"]:
            band.SetUnitType(str(info["unit"]))
        if info["srs_wkt"]:
            from osgeo import osr

            srs = osr.SpatialReference(wkt=info["srs_wkt"])
            out.SetSpatialRef(srs)
        band.FlushCache()
        out.FlushCache()
    finally:
        del out

    # 坐标数组：剩余两维与被选维度的坐标值如实入 sidecar；绝不外插。
    remaining_names = [d for i, d in enumerate(dims) if i not in selected]
    selected_detail: list[dict[str, Any]] = []
    coords: dict[str, list[float]] = {}
    try:
        coord_holder = gdal.OpenEx(str(in_path), gdal.OF_MULTIDIM_RASTER | gdal.OF_READONLY)
        rg2 = coord_holder.GetRootGroup()
        for dim_name in dims:
            coord = rg2.OpenMDArray(dim_name)
            if coord is None:
                continue
            vals = coord.ReadAsArray()
            if vals is None:
                continue
            flat = [float(x) for x in vals.ravel() if not isinstance(x, bytes)]
            pos = dims.index(dim_name)
            if pos in selected:
                idx = selected[pos]
                selected_detail.append(
                    {
                        "dim": dim_name,
                        "index": idx,
                        "value": flat[idx] if idx < len(flat) else None,
                    }
                )
            elif dim_name in remaining_names:
                expected_size = ny if remaining_names[0] == dim_name else nx
                if vals.size == expected_size:
                    coords[dim_name] = flat
    except Exception:
        # 坐标读取是尽力而为的元数据增益，读不到就如实缺席（不编造）
        selected_detail = [
            {"dim": dim_name, "index": selected[dims.index(dim_name)], "value": None}
            for dim_name in dims
            if dims.index(dim_name) in selected
        ]
        coords = {}

    sidecar = {
        "schema_version": "1.0",
        "capability": "gdal:mdim_slice",
        "source": {
            "path": str(in_path),
            "array": info["name"],
            "dims": dims,
            "shape": shape,
        },
        "selected": selected_detail,
        "remaining_dims": [
            {"dim": d, "size": int(s)} for d, s in zip(remaining_names, (ny, nx), strict=True)
        ],
        "coords": coords,
        "unit": info["unit"],
        "scale": info["scale"],
        "offset": info["offset"],
        "nodata": info["nodata"],
        "dtype": str(nd.dtype),
        "value_semantics": "raw（原值；物理值 = raw×scale+offset，由调用方决定是否换算）",
        "crs_wkt": info["srs_wkt"],
        "georeferenced": info["srs_wkt"] is not None,
    }
    sidecar_path = staged_path.with_name(staged_path.name + ".mdim_slice.json")
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "sidecar": str(sidecar_path),
        "shape": [ny, nx],
        "selected": selected_detail,
        "unit": info["unit"],
        "nodata": info["nodata"],
        "georeferenced": info["srs_wkt"] is not None,
    }
