"""UI 图层领域模型（P3-01 / P3-02）。

纯 Python 模型，严格遵守 GUI 进程隔离要求，零原生地理库导入。
元数据统一由后台 Worker 进程通过 probe_dataset 提供。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LayerItem:
    """栅格影像图层对象。"""

    name: str
    file_path: Path
    metadata: dict[str, Any]
    layer_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    visible: bool = True
    opacity: float = 1.0

    # 显示配置
    display_mode: str = "gray"  # 'gray' 或 'rgb'
    gray_band: int = 1
    rgb_bands: tuple[int, int, int] = (1, 1, 1)

    def __post_init__(self) -> None:
        self.file_path = Path(self.file_path).resolve()
        count = self.band_count
        if count >= 3:
            self.display_mode = "rgb"
            self.rgb_bands = (1, 2, 3)
        else:
            self.display_mode = "gray"
            self.gray_band = 1
            self.rgb_bands = (1, 1, 1)

    @property
    def width(self) -> int:
        return int(self.metadata.get("width", 0))

    @property
    def height(self) -> int:
        return int(self.metadata.get("height", 0))

    @property
    def band_count(self) -> int:
        return int(self.metadata.get("count", 1))

    @property
    def driver(self) -> str:
        return str(self.metadata.get("driver", "Unknown"))

    @property
    def crs_epsg(self) -> int | None:
        return self.metadata.get("crs_epsg")

    @property
    def crs_display(self) -> str:
        epsg = self.crs_epsg
        if epsg:
            return f"EPSG:{epsg}"
        wkt = self.metadata.get("crs_wkt")
        if wkt:
            return "自定义坐标系"
        return "无投影 (像元坐标)"

    @property
    def bounds(self) -> list[float] | None:
        return self.metadata.get("bounds")

    @property
    def dtypes(self) -> list[str]:
        return self.metadata.get("dtypes", [])

    @property
    def nodata(self) -> float | None:
        return self.metadata.get("nodata")

    def get_selected_bands(self) -> list[int]:
        """获取当前显示模式所需读取的波段列表（1-based）。"""
        if self.display_mode == "rgb":
            return list(self.rgb_bands)
        return [self.gray_band]
