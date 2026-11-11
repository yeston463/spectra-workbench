"""UI 图层领域模型（P3-01 / P3-02 / P3-04 / P3-05）。

纯 Python：不导入 Qt、不导入任何原生地理库。元数据一律来自后台 worker 的 probe_dataset。

`display_mode` / `gray_band` / `rgb_bands` / `opacity` 是 `display: DisplaySettings` 的代理属性，
保留旧调用点（图层面板与既有测试）的同时，让显示参数成为可整体序列化的对象。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spectra_workbench.domain.paths import is_vsi_or_remote, normalize_vsi_path
from spectra_workbench.domain.roi import ROI
from spectra_workbench.rendering.display import DisplayMode, DisplaySettings


@dataclass
class LayerItem:
    """栅格影像图层。"""

    name: str
    file_path: Path | str
    metadata: dict[str, Any]
    layer_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    visible: bool = True
    display: DisplaySettings = field(default_factory=DisplaySettings)
    rois: list[ROI] = field(default_factory=list)
    provider_hint: str | None = None
    credential_profile: str | None = None

    def __post_init__(self) -> None:
        if is_vsi_or_remote(self.file_path):
            self.file_path = normalize_vsi_path(self.file_path)
        else:
            self.file_path = Path(self.file_path).resolve()
        count = self.band_count
        if count >= 3:
            self.display.mode = DisplayMode.rgb
            self.display.rgb_bands = (1, 2, 3)
        else:
            self.display.mode = DisplayMode.gray
            self.display.gray_band = 1
            self.display.rgb_bands = (1, 1, 1)

    # ------------------------------------------------------------ 显示代理
    @property
    def display_mode(self) -> str:
        return str(self.display.mode.value)

    @display_mode.setter
    def display_mode(self, value: str) -> None:
        self.display.mode = DisplayMode(value)

    @property
    def gray_band(self) -> int:
        return int(self.display.gray_band)

    @gray_band.setter
    def gray_band(self, value: int) -> None:
        self.display.gray_band = int(value)

    @property
    def rgb_bands(self) -> tuple[int, int, int]:
        return tuple(int(v) for v in self.display.rgb_bands)  # type: ignore[return-value]

    @rgb_bands.setter
    def rgb_bands(self, value: tuple[int, int, int]) -> None:
        self.display.rgb_bands = (int(value[0]), int(value[1]), int(value[2]))

    @property
    def opacity(self) -> float:
        return float(self.display.opacity)

    @opacity.setter
    def opacity(self, value: float) -> None:
        self.display.opacity = float(value)

    # ------------------------------------------------------------ 元数据访问
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
    def crs_wkt(self) -> str | None:
        return self.metadata.get("crs_wkt")

    @property
    def crs_display(self) -> str:
        epsg = self.crs_epsg
        if epsg:
            return f"EPSG:{epsg}"
        if self.crs_wkt:
            return "自定义坐标系"
        return "无投影 (像元坐标)"

    @property
    def has_geotransform(self) -> bool:
        return bool(self.metadata.get("has_geotransform", self.metadata.get("geotransform")))

    @property
    def geotransform(self) -> list[float] | None:
        return self.metadata.get("geotransform")

    @property
    def bounds(self) -> list[float] | None:
        return self.metadata.get("bounds")

    @property
    def dtypes(self) -> list[str]:
        return self.metadata.get("dtypes", [])

    @property
    def nodata(self) -> float | None:
        return self.metadata.get("nodata")

    @property
    def overview_levels(self) -> list[int]:
        return [int(v) for v in self.metadata.get("overview_levels", [])]

    @property
    def band_mask_flags(self) -> list[int]:
        return [int(b.get("mask_flags", 0)) for b in self.metadata.get("bands", [])]

    @property
    def has_alpha(self) -> bool:
        return any(bool(b.get("has_alpha")) for b in self.metadata.get("bands", []))

    @property
    def has_mask(self) -> bool:
        bands = self.metadata.get("bands", [])
        return (
            any(int(b.get("mask_flags", 0)) not in (0, 1) for b in bands)
            if bands
            else bool(self.metadata.get("has_mask_band"))
        )

    def scale_offset(self, band_index: int) -> tuple[float, float]:
        for b in self.metadata.get("bands", []):
            if int(b.get("band", 0)) == band_index:
                return (
                    float(b["scale"]) if b.get("scale") is not None else 1.0,
                    float(b["offset"]) if b.get("offset") is not None else 0.0,
                )
        return (1.0, 0.0)

    def get_selected_bands(self) -> list[int]:
        return self.display.selected_bands(self.band_count)
