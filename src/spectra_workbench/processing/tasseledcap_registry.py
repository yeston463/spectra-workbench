"""P6-03 缨帽变换系数登记表（纯 Python，可被 GUI 与 Worker 共用）。

设计原则
--------
- **系数必须有来源**：每个传感器条目同时记录原始论文、DOI/出版页与产品级别；
- **传感器必须显式匹配**：调用方必须给出 6 个输入波段号（按系数顺序），
  不做「猜波段」或静默回退；
- **只登记有据可查的传感器**：未登记传感器直接拒绝，绝不挪用其它传感器的系数。

系数表与来源（逐项核对，禁止凭印象扩展）
----------------------------------------
- Landsat 5 TM：Crist (1985) 的 TM reflectance-factor 等价变换矩阵，
  见 Remote Sensing of Environment 17(3):301-306，DOI 10.1016/0034-4257(85)90102-6。
- Landsat 7 ETM+：Huang et al. (2002) 基于 at-satellite reflectance 的系数，
  见 International Journal of Remote Sensing 23(8):1741-1748。
- Landsat 8 OLI：Baig et al. (2014) 基于 at-satellite reflectance 的系数，
  见 Remote Sensing Letters 5(5):423-431，DOI 10.1080/2150704X.2014.915434。

本模块只存元数据，不导入 numpy/GDAL；实际矩阵运算在 tasseledcap_ops 中。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "TASSELED_CAP_SENSORS",
    "TasseledCapSensor",
    "get_sensor",
    "sensor_keys",
    "sensor_source_line",
]


@dataclass(frozen=True)
class TasseledCapSensor:
    """一个传感器的缨帽系数及可审计来源。"""

    key: str
    label: str
    platform: str
    product_level: str
    band_order: tuple[str, ...]
    coefficients: tuple[tuple[float, ...], ...]
    output_bands: tuple[str, ...]
    source: str
    source_url: str
    notes: str


#: 亮度 / 绿度 / 湿度三行系数；每行 6 个权重，顺序与 band_order 一致。
TASSELED_CAP_SENSORS: dict[str, TasseledCapSensor] = {
    "landsat5_tm": TasseledCapSensor(
        key="landsat5_tm",
        label="Landsat 5 TM（Crist 1985 reflectance factor）",
        platform="Landsat 5 Thematic Mapper",
        product_level="reflectance_factor",
        band_order=("Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"),
        coefficients=(
            (0.2043, 0.4158, 0.5524, 0.5741, 0.3124, 0.2303),
            (-0.1603, -0.2819, -0.4934, 0.7940, -0.0002, -0.1446),
            (0.0315, 0.2021, 0.3102, 0.1594, -0.6806, -0.6109),
        ),
        output_bands=("brightness", "greenness", "wetness"),
        source=(
            "Crist, E.P. (1985). A TM Tasseled Cap equivalent transformation for "
            "reflectance factor data. Remote Sensing of Environment 17(3):301-306."
        ),
        source_url="https://doi.org/10.1016/0034-4257(85)90102-6",
        notes=(
            "输入应为 TM 波段的 reflectance factor（反射率因子）；"
            "系数顺序为 TM 1,2,3,4,5,7。"
        ),
    ),
    "landsat7_etm": TasseledCapSensor(
        key="landsat7_etm",
        label="Landsat 7 ETM+（Huang et al. 2002 at-satellite reflectance）",
        platform="Landsat 7 Enhanced Thematic Mapper Plus",
        product_level="at_satellite_reflectance",
        band_order=("Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"),
        coefficients=(
            (0.3561, 0.3972, 0.3904, 0.6966, 0.2286, 0.1596),
            (-0.3344, -0.3544, -0.4556, 0.6966, -0.0242, -0.2630),
            (0.2626, 0.2141, 0.0926, 0.0656, -0.7629, -0.5388),
        ),
        output_bands=("brightness", "greenness", "wetness"),
        source=(
            "Huang, C., Wylie, B., Yang, L., Homer, C., Zylstra, G. (2002). "
            "Derivation of a tasselled cap transformation based on Landsat 7 "
            "at-satellite reflectance. International Journal of Remote Sensing "
            "23(8):1741-1748."
        ),
        source_url="https://pubs.usgs.gov/publication/70024815",
        notes=(
            "输入应为 ETM+ 的大气顶（at-satellite）反射率；"
            "系数顺序为 ETM+ 1,2,3,4,5,7。"
        ),
    ),
    "landsat8_oli": TasseledCapSensor(
        key="landsat8_oli",
        label="Landsat 8 OLI（Baig et al. 2014 at-satellite reflectance）",
        platform="Landsat 8 Operational Land Imager",
        product_level="at_satellite_reflectance",
        band_order=("Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"),
        coefficients=(
            (0.3029, 0.2786, 0.4733, 0.5599, 0.5080, 0.1872),
            (-0.2941, -0.2430, -0.5424, 0.7276, 0.0713, -0.1608),
            (0.1511, 0.1973, 0.3283, 0.3407, -0.7117, -0.4559),
        ),
        output_bands=("brightness", "greenness", "wetness"),
        source=(
            "Baig, M.H.A., Zhang, L., Shuai, T., Tong, Q. (2014). Derivation of a "
            "tasselled cap transformation based on Landsat 8 at-satellite "
            "reflectance. Remote Sensing Letters 5(5):423-431."
        ),
        source_url="https://doi.org/10.1080/2150704X.2014.915434",
        notes=(
            "输入应为 OLI 的大气顶（at-satellite）反射率；"
            "系数顺序为 OLI 2,3,4,5,6,7（Blue, Green, Red, NIR, SWIR1, SWIR2）。"
        ),
    ),
}


def sensor_keys() -> tuple[str, ...]:
    """已登记传感器 key（顺序即 GUI 下拉顺序）。"""
    return tuple(TASSELED_CAP_SENSORS)


def get_sensor(key: str) -> TasseledCapSensor:
    """按 key 取传感器；未知 key 抛 ValueError（绝不回退到其它传感器）。"""
    normalized = str(key or "").strip().lower()
    try:
        return TASSELED_CAP_SENSORS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"未登记的缨帽传感器 {key!r}；可用：{', '.join(sensor_keys())}"
        ) from exc


def sensor_source_line(key: str) -> str:
    """GUI 展示用的来源摘要（含产品级别与来源链接）。"""
    sensor = get_sensor(key)
    return (
        f"产品级别：{sensor.product_level}；波段顺序：{', '.join(sensor.band_order)}；"
        f"来源：{sensor.source} {sensor.source_url}"
    )
