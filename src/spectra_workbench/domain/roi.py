"""ROI 几何模型（P3-05）。

纯 Python：不依赖 Qt 与地理库，可脱离界面单测。

坐标空间约定（必须显式，否则旋转影像会串味）：
- `coordinate_space="pixel"`：(col, row) 像元坐标，画布直接用；
- `coordinate_space="map"`：地图坐标，需同时给出 `crs_wkt`。
两者互转由 worker 完成（GUI 不做坐标运算）。
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ROIShape(StrEnum):
    rectangle = "rectangle"
    polygon = "polygon"


class ROIError(ValueError):
    pass


class ROI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roi_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "ROI"
    shape: ROIShape = ROIShape.rectangle
    coordinate_space: str = "pixel"  # pixel | map
    crs_wkt: str | None = None
    # rectangle: 恰好 2 个对角点；polygon: >=3 个顶点（不重复首点）
    points: list[tuple[float, float]] = Field(default_factory=list)
    pixel_convention: str = "corner"
    attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ROI:
        if self.coordinate_space not in ("pixel", "map"):
            raise ROIError(f"未知坐标空间：{self.coordinate_space}")
        if self.coordinate_space == "map" and not self.crs_wkt:
            raise ROIError("map 空间的 ROI 必须记录 crs_wkt，否则无法复现位置")
        for p in self.points:
            if len(p) != 2:
                raise ROIError(f"顶点必须是 (x, y) 二元组，收到 {p}")
        if self.shape is ROIShape.rectangle:
            if len(self.points) != 2:
                raise ROIError(f"矩形需要 2 个对角点，收到 {len(self.points)} 个")
            if self.points[0] == self.points[1]:
                raise ROIError("矩形对角点重合，面积为 0")
        else:
            if len(self.points) < 3:
                raise ROIError(f"多边形至少 3 个顶点，收到 {len(self.points)} 个")
        return self

    # ------------------------------------------------------------------
    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y)。"""
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    def rectangle_window(self) -> tuple[int, int, int, int]:
        """矩形 -> (x_off, y_off, win_w, win_h) 像元窗口（向上取整，至少 1）。"""
        if self.shape is not ROIShape.rectangle:
            raise ROIError("只有矩形 ROI 能直接转成像元窗口")
        (x0, y0, x1, y1) = self.bounds
        import math

        ox = max(0, int(math.floor(min(x0, x1))))
        oy = max(0, int(math.floor(min(y0, y1))))
        ww = max(1, int(math.ceil(max(x0, x1))) - ox)
        wh = max(1, int(math.ceil(max(y0, y1))) - oy)
        return (ox, oy, ww, wh)

    def contains(self, x: float, y: float) -> bool:
        (x0, y0, x1, y1) = self.bounds
        if self.shape is ROIShape.rectangle:
            return x0 <= x <= x1 and y0 <= y <= y1
        return self._point_in_polygon(x, y)

    def _point_in_polygon(self, x: float, y: float) -> bool:
        inside = False
        n = len(self.points)
        for i in range(n):
            x1, y1 = self.points[i]
            x2, y2 = self.points[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                xint = x2 + (y - y2) * (x1 - x2) / (y1 - y2) if y1 != y2 else x1
                if x < xint:
                    inside = not inside
        return inside

    def area_pixels(self) -> float:
        """像元面积。多边形用鞋带公式。"""
        if self.shape is ROIShape.rectangle:
            (x0, y0, x1, y1) = self.bounds
            return abs(x1 - x0) * abs(y1 - y0)
        s = 0.0
        n = len(self.points)
        for i in range(n):
            x1, y1 = self.points[i]
            x2, y2 = self.points[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    # ------------------------------------------------------------------
    def moved(self, dcol: float, drow: float) -> ROI:
        return self.model_copy(update={"points": [(p[0] + dcol, p[1] + drow) for p in self.points]})

    def with_vertex(self, index: int, col: float, row: float) -> ROI:
        if index < 0 or index >= len(self.points):
            raise ROIError(f"顶点序号 {index} 越界（共 {len(self.points)} 个）")
        pts = list(self.points)
        pts[index] = (float(col), float(row))
        if self.shape is ROIShape.rectangle and pts[0] == pts[1]:
            raise ROIError("矩形对角点不能重合")
        return self.model_copy(update={"points": pts})

    def to_geojson(self, pixel_to_map) -> dict:
        """导出 GeoJSON。`pixel_to_map(col,row)->(x,y)` 由调用方提供（worker 实现）。"""
        if self.shape is ROIShape.rectangle:
            (c0, r0), (c1, r1) = self.points
            ring_px = [(c0, r0), (c1, r0), (c1, r1), (c0, r1), (c0, r0)]
        else:
            ring_px = list(self.points) + [self.points[0]]
        coords = [list(pixel_to_map(c, r)) for (c, r) in ring_px]
        geom = {"type": "Polygon", "coordinates": [coords]}
        return {
            "type": "Feature",
            "properties": {"roi_id": self.roi_id, "name": self.name, **self.attributes},
            "geometry": geom,
        }
