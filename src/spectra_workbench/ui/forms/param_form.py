"""从注册表动态构建算法与工具参数面板（P2-04）。

特性：
1. 完整接通 GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换。
2. 针对未接入平台的能力，保留完整说明、限制项与状态徽标，严格禁用提交按钮（禁止提交）。
3. 严格遵守 GUI 进程隔离要求，零原生地理库导入。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.domain.capability_wiring import (
    CONNECTED_CAPABILITIES,
    is_multi_input,
    is_vector_output,
    supports_output_format,
)
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
)
from spectra_workbench.processing.output_formats import (
    validate_output_format,
    validate_vector_output_format,
)
from spectra_workbench.processing.raster_params import (
    DIFF_MODES,
    FUSION_METHODS,
    FVC_ENDMEMBER_MODES,
    GCP_TRANSFORMS,
    LOCAL_RESAMPLES,
    RESAMPLE_ALGORITHM_CHOICES,
    ParameterError,
    normalize_raster_params,
    sensor_keys,
    supported_params_for,
)
from spectra_workbench.processing.tasseledcap_registry import (
    get_sensor,
    sensor_source_line,
)

if TYPE_CHECKING:
    from spectra_workbench.domain.models import CapabilityDescriptor


#: 输出格式下拉的候选项按能力族切换：
#: 栅格成果走栅格驱动矩阵，矢量成果（P5-02 polygonize/contour）走 OGR 驱动。
_RASTER_FORMAT_ITEMS = ["GTiff (GeoTIFF)", "ENVI (.dat/.hdr)", "COG (云优化 GeoTIFF)", "PNG", "JPEG"]
_VECTOR_FORMAT_ITEMS = ["GPKG (GeoPackage)", "GeoJSON (.geojson)", "Shapefile (.shp)", "FlatGeobuf (.fgb)"]




class CapabilityFormWidget(QWidget):
    """能力参数动态表单控件。"""

    sig_task_submitted = Signal(object)  # TaskSpec

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_cap: CapabilityDescriptor | None = None
        self._project_root = Path.cwd()
        self._metadata_provider: Callable[[Path], dict[str, Any]] | None = None

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.form_layout = QVBoxLayout(scroll_content)
        self.form_layout.setContentsMargins(4, 4, 4, 4)
        self.form_layout.setSpacing(10)

        # 1. 顶部能力信息卡片
        self.grp_header = QGroupBox("能力概览 (Capability Info)")
        header_layout = QVBoxLayout(self.grp_header)
        header_layout.setSpacing(4)

        self.lbl_title = QLabel("请在左侧工具箱选择算法或命令")
        self.lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #2c3e50;")
        self.lbl_badges = QLabel("")
        self.lbl_desc = QLabel("选择一项能力后，此处将自动呈现参数配置或状态说明。")
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setStyleSheet("color: #555;")

        header_layout.addWidget(self.lbl_title)
        header_layout.addWidget(self.lbl_badges)
        header_layout.addWidget(self.lbl_desc)
        self.form_layout.addWidget(self.grp_header)

        # 2. 状态警示条（针对未接入或缺失的能力）
        self.lbl_unsupported_banner = QLabel()
        self.lbl_unsupported_banner.setWordWrap(True)
        self.lbl_unsupported_banner.setStyleSheet(
            "background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; "
            "padding: 8px; border-radius: 4px; font-weight: bold;"
        )
        self.lbl_unsupported_banner.setVisible(False)
        self.form_layout.addWidget(self.lbl_unsupported_banner)

        # 3. 动态参数表单区
        self.grp_params = QGroupBox("参数配置 (Parameters)")
        self.params_form = QFormLayout(self.grp_params)
        self.params_form.setSpacing(8)

        # 通用参数控件
        self.txt_input = QLineEdit()
        self.btn_browse_in = QPushButton("浏览...")
        self.btn_browse_in.clicked.connect(self._browse_input)
        in_row = QHBoxLayout()
        in_row.addWidget(self.txt_input, stretch=1)
        in_row.addWidget(self.btn_browse_in)
        self.lbl_input = QLabel("输入影像 (Input):")
        self.params_form.addRow(self.lbl_input, in_row)

        # --- 多输入（P4-02 拼接 / VRT / 堆叠）---
        # 顺序即输入顺序，而输入顺序决定重叠优先级（GDAL「后加入者优先」），
        # 因此必须可增删且可上下移动，不能只做一个无序集合。
        self.lbl_inputs = QLabel(
            "输入源列表（顺序决定重叠优先级：first=先列的胜，last=后列的胜）:"
        )
        self.lst_inputs = QListWidget()
        self.lst_inputs.setMinimumHeight(90)
        self.btn_add_input = QPushButton("添加...")
        self.btn_del_input = QPushButton("移除")
        self.btn_up_input = QPushButton("上移")
        self.btn_down_input = QPushButton("下移")
        self.btn_add_input.clicked.connect(self._add_input_files)
        self.btn_del_input.clicked.connect(self._remove_selected_input)
        self.btn_up_input.clicked.connect(lambda: self._move_selected_input(-1))
        self.btn_down_input.clicked.connect(lambda: self._move_selected_input(1))
        in_btns = QHBoxLayout()
        for b in (self.btn_add_input, self.btn_del_input, self.btn_up_input, self.btn_down_input):
            in_btns.addWidget(b)
        in_btns.addStretch(1)

        self.grp_inputs = QGroupBox("多输入源")
        gi_layout = QVBoxLayout(self.grp_inputs)
        gi_layout.addWidget(self.lbl_inputs)
        gi_layout.addWidget(self.lst_inputs)
        gi_layout.addLayout(in_btns)
        self.params_form.addRow(self.grp_inputs)

        self.txt_output = QLineEdit()
        self.btn_browse_out = QPushButton("保存为...")
        self.btn_browse_out.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.txt_output, stretch=1)
        out_row.addWidget(self.btn_browse_out)
        self.params_form.addRow("输出成果 (Output):", out_row)

        self.chk_overwrite = QCheckBox("允许覆盖已有文件 (Overwrite)")
        self.chk_overwrite.setChecked(False)
        self.params_form.addRow("", self.chk_overwrite)

        # 专有参数行
        # --- 格式选择 ---
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["GTiff (GeoTIFF)", "ENVI (.dat/.hdr)", "COG (云优化 GeoTIFF)", "PNG", "JPEG"])
        self.lbl_format = QLabel("输出格式 (Format):")
        self.params_form.addRow(self.lbl_format, self.cmb_format)

        # --- P4-02 拼接 / VRT / 堆叠 / 波段管理 ---
        # 重叠优先级：GDAL 的重叠区取**后加入**的源，故 first 由反转顺序实现。
        self.cmb_overlap_priority = QComboBox()
        self.cmb_overlap_priority.addItem("后列的源优先 (last)", "last")
        self.cmb_overlap_priority.addItem("先列的源优先 (first)", "first")
        self.lbl_overlap_priority = QLabel("重叠优先级 (overlapPriority):")
        self.params_form.addRow(self.lbl_overlap_priority, self.cmb_overlap_priority)

        self.cmb_resolution = QComboBox()
        for label, val in (
            ("自动 (不指定)", ""),
            ("沿用第一个源 (first)", "first"),
            ("沿用最后一个源 (last)", "last"),
            ("取最高 (highest)", "highest"),
            ("取最低 (lowest)", "lowest"),
            ("取平均 (average)", "average"),
            ("估算 (estimate)", "estimate"),
            ("取相同 (same)", "same"),
        ):
            self.cmb_resolution.addItem(label, val)
        self.lbl_resolution = QLabel("VRT 分辨率策略 (resolution):")
        self.params_form.addRow(self.lbl_resolution, self.cmb_resolution)

        # NoData 用 QLineEdit：必须能区分「未设置（留空）」与「设置为 0（填 0）」。
        # 用 QSpinBox 无法表达「未设置」，会让 dstNodata=0 被当作未设置丢掉。
        self.txt_src_nodata = QLineEdit()
        self.txt_src_nodata.setPlaceholderText("留空=未设置；填 0 表示 NoData 为 0")
        self.lbl_src_nodata = QLabel("源 NoData (srcNodata):")
        self.params_form.addRow(self.lbl_src_nodata, self.txt_src_nodata)

        self.txt_vrt_nodata = QLineEdit()
        self.txt_vrt_nodata.setPlaceholderText("留空=未设置；填 0 表示 NoData 为 0")
        self.lbl_vrt_nodata = QLabel("VRT NoData (vrtNodata):")
        self.params_form.addRow(self.lbl_vrt_nodata, self.txt_vrt_nodata)

        self.txt_dst_nodata = QLineEdit()
        self.txt_dst_nodata.setPlaceholderText(
            "留空=不重映射；填 0 表示把无效像元重映射为 0（三者一致）"
        )
        self.lbl_dst_nodata = QLabel("输出 NoData (dstNodata):")
        self.params_form.addRow(self.lbl_dst_nodata, self.txt_dst_nodata)

        self.chk_separate = QCheckBox("每个源独立成波段（separate，用于堆叠）")
        self.lbl_separate = QLabel("独立波段 (separate):")
        self.params_form.addRow(self.lbl_separate, self.chk_separate)

        self.chk_relative_paths = QCheckBox("源引用使用相对路径（relativePaths，推荐）")
        self.chk_relative_paths.setChecked(True)
        self.lbl_relative_paths = QLabel("相对路径 (relativePaths):")
        self.params_form.addRow(self.lbl_relative_paths, self.chk_relative_paths)

        self.txt_band_list = QLineEdit()
        self.txt_band_list.setPlaceholderText("留空=保留全部；例：3,1,2 表示按此顺序重排")
        self.lbl_band_list = QLabel("波段顺序 (bandList):")
        self.params_form.addRow(self.lbl_band_list, self.txt_band_list)

        # --- P5-02 地形分析 ---
        self.cmb_dem_processing = QComboBox()
        for label, val in (
            ("山体阴影 (hillshade)", "hillshade"),
            ("坡度 (slope)", "slope"),
            ("坡向 (aspect)", "aspect"),
            ("地形 ruggedness 指数 (TRI)", "TRI"),
            ("地形位置指数 (TPI)", "TPI"),
            ("粗糙度 (roughness)", "roughness"),
        ):
            self.cmb_dem_processing.addItem(label, val)
        self.lbl_dem_processing = QLabel("地形模式 (processing):")
        self.params_form.addRow(self.lbl_dem_processing, self.cmb_dem_processing)

        self.spin_dem_zfactor = QDoubleSpinBox()
        self.spin_dem_zfactor.setDecimals(6)
        self.spin_dem_zfactor.setRange(-1e9, 1e9)
        self.spin_dem_zfactor.setSingleStep(0.1)
        self.spin_dem_zfactor.setValue(1.0)
        self.lbl_dem_zfactor = QLabel("高程缩放 (zFactor):")
        self.params_form.addRow(self.lbl_dem_zfactor, self.spin_dem_zfactor)

        self.spin_dem_scale = QDoubleSpinBox()
        self.spin_dem_scale.setDecimals(6)
        self.spin_dem_scale.setRange(1e-6, 1e12)
        self.spin_dem_scale.setSingleStep(0.1)
        self.spin_dem_scale.setValue(1.0)
        self.lbl_dem_scale = QLabel("垂直比例 (scale):")
        self.params_form.addRow(self.lbl_dem_scale, self.spin_dem_scale)

        # --- P5-02 矢量栅格化 ---
        self.txt_burn_values = QLineEdit()
        self.txt_burn_values.setPlaceholderText("例：7；多波段例：7,8（与 attribute 互斥）")
        self.lbl_burn_values = QLabel("烧录像元值 (burnValues):")
        self.params_form.addRow(self.lbl_burn_values, self.txt_burn_values)

        self.txt_attribute = QLineEdit()
        self.txt_attribute.setPlaceholderText("按字段值烧录，例：value（与 burnValues 互斥）")
        self.lbl_attribute = QLabel("烧录字段 (attribute):")
        self.params_form.addRow(self.lbl_attribute, self.txt_attribute)

        self.chk_all_touched = QCheckBox("包含所有触及像元（allTouched）")
        self.lbl_all_touched = QLabel("全触及像元 (allTouched):")
        self.params_form.addRow(self.lbl_all_touched, self.chk_all_touched)

        # --- P5-02 散点栅格插值 ---
        self.cmb_grid_algorithm = QComboBox()
        for label, val in (
            ("反距离加权 (invdist)", "invdist"),
            ("反距离加权最近邻 (invdistnn)", "invdistnn"),
            ("滑动平均 (average)", "average"),
            ("最近邻 (nearest)", "nearest"),
            ("线性 (linear)", "linear"),
        ):
            self.cmb_grid_algorithm.addItem(label, val)
        self.lbl_grid_algorithm = QLabel("插值算法 (algorithm):")
        self.params_form.addRow(self.lbl_grid_algorithm, self.cmb_grid_algorithm)

        self.txt_zfield = QLineEdit()
        self.txt_zfield.setPlaceholderText("留空=读几何 Z；例：value")
        self.lbl_zfield = QLabel("高程字段 (zField):")
        self.params_form.addRow(self.lbl_zfield, self.txt_zfield)

        # --- P5-02 栅格矢量化（成果为矢量）---
        self.txt_field_name = QLineEdit()
        self.txt_field_name.setPlaceholderText("像元值保存字段名，默认 DN")
        self.txt_field_name.setText("DN")
        self.lbl_field_name = QLabel("值字段名 (fieldName):")
        self.params_form.addRow(self.lbl_field_name, self.txt_field_name)

        self.cmb_connectedness = QComboBox()
        self.cmb_connectedness.addItem("4 邻域 (connectedness=4)", 4)
        self.cmb_connectedness.addItem("8 邻域 (connectedness=8)", 8)
        self.lbl_connectedness = QLabel("连通性 (connectedness):")
        self.params_form.addRow(self.lbl_connectedness, self.cmb_connectedness)

        # --- P5-02 矢量化输入波段（polygonize/contour 共用）---
        self.spin_band = QSpinBox()
        self.spin_band.setRange(1, 999)
        self.spin_band.setValue(1)
        self.lbl_band = QLabel("输入波段 (band，1-based):")
        self.params_form.addRow(self.lbl_band, self.spin_band)

        # --- P5-02 等值线（成果为矢量）---
        self.spin_contour_interval = QDoubleSpinBox()
        self.spin_contour_interval.setDecimals(6)
        self.spin_contour_interval.setRange(0.0, 1e9)
        self.spin_contour_interval.setSingleStep(1.0)
        self.spin_contour_interval.setValue(10.0)
        self.lbl_contour_interval = QLabel("等值线步长 (interval，必填>0):")
        self.params_form.addRow(self.lbl_contour_interval, self.spin_contour_interval)

        self.spin_contour_offset = QDoubleSpinBox()
        self.spin_contour_offset.setDecimals(6)
        self.spin_contour_offset.setRange(-1e9, 1e9)
        self.spin_contour_offset.setSingleStep(1.0)
        self.spin_contour_offset.setValue(0.0)
        self.lbl_contour_offset = QLabel("基准高程 (offset，0 合法):")
        self.params_form.addRow(self.lbl_contour_offset, self.spin_contour_offset)

        self.txt_fixed_levels = QLineEdit()
        self.txt_fixed_levels.setPlaceholderText(
            "留空=按 interval 生成；填逗号分隔高程则完全替代，例：2.5,5.5,11"
        )
        self.lbl_fixed_levels = QLabel("固定等级 (fixedLevels):")
        self.params_form.addRow(self.lbl_fixed_levels, self.txt_fixed_levels)

        # --- P5-04 矢量处理 / SQL / 坐标转换（ogr:vectortranslate）---
        self.txt_vt_sql = QLineEdit()
        self.txt_vt_sql.setPlaceholderText(
            "OGR SQL，例：SELECT name, value FROM points WHERE value >= 50"
        )
        self.lbl_vt_sql = QLabel("SQL 查询 (sql):")
        self.params_form.addRow(self.lbl_vt_sql, self.txt_vt_sql)

        self.txt_vt_where = QLineEdit()
        self.txt_vt_where.setPlaceholderText("留空=不过滤；例：value >= 50 AND name LIKE 'p1%'")
        self.lbl_vt_where = QLabel("属性过滤 (where):")
        self.params_form.addRow(self.lbl_vt_where, self.txt_vt_where)

        self.txt_vt_spat = QLineEdit()
        self.txt_vt_spat.setPlaceholderText("留空=不过滤；按源 CRS 单位，例：0,0,3,3")
        self.lbl_vt_spat = QLabel("空间过滤框 (spat):")
        self.params_form.addRow(self.lbl_vt_spat, self.txt_vt_spat)

        self.txt_vt_select = QLineEdit()
        self.txt_vt_select.setPlaceholderText("留空=保留全部字段；例：name,value")
        self.lbl_vt_select = QLabel("保留字段 (select):")
        self.params_form.addRow(self.lbl_vt_select, self.txt_vt_select)

        self.txt_vt_dst_srs = QLineEdit()
        self.txt_vt_dst_srs.setPlaceholderText("留空=保持源 CRS；例：EPSG:4326（输出 X=经度, Y=纬度）")
        self.lbl_vt_dst_srs = QLabel("目标 CRS (dstSRS):")
        self.params_form.addRow(self.lbl_vt_dst_srs, self.txt_vt_dst_srs)

        # --- P6-01 光谱指数 ---
        self.cmb_sindex = QComboBox()
        for label, val in (
            ("NDVI = (NIR−Red)/(NIR+Red)", "ndvi"),
            ("NDWI = (Green−NIR)/(Green+NIR)", "ndwi"),
            ("NBR = (NIR−SWIR)/(NIR+SWIR)", "nbr"),
            ("SAVI = (NIR−Red)/(NIR+Red+L)×(1+L)", "savi"),
        ):
            self.cmb_sindex.addItem(label, val)
        self.lbl_sindex = QLabel("光谱指数 (index):")
        self.params_form.addRow(self.lbl_sindex, self.cmb_sindex)

        self.spin_band_a = QSpinBox()
        self.spin_band_a.setRange(1, 999)
        self.spin_band_a.setValue(1)
        self.lbl_band_a = QLabel("波段 A (bandA，ndvi/nbr/savi=NIR，ndwi=Green):")
        self.params_form.addRow(self.lbl_band_a, self.spin_band_a)

        self.spin_band_b = QSpinBox()
        self.spin_band_b.setRange(1, 999)
        self.spin_band_b.setValue(2)
        self.lbl_band_b = QLabel("波段 B (bandB，ndvi/savi=Red，ndwi=NIR，nbr=SWIR):")
        self.params_form.addRow(self.lbl_band_b, self.spin_band_b)

        self.spin_savi_l = QDoubleSpinBox()
        self.spin_savi_l.setDecimals(4)
        self.spin_savi_l.setRange(0.0, 1.0)
        self.spin_savi_l.setSingleStep(0.1)
        self.spin_savi_l.setValue(0.5)
        self.lbl_savi_l = QLabel("SAVI 土壤调节因子 L (saviL，仅 savi):")
        self.params_form.addRow(self.lbl_savi_l, self.spin_savi_l)

        self.chk_use_physical = QCheckBox("使用波段元数据 scale/offset 换算物理量（推荐）")
        self.chk_use_physical.setChecked(True)
        self.lbl_use_physical = QLabel("物理量 (usePhysical):")
        self.params_form.addRow(self.lbl_use_physical, self.chk_use_physical)

        # --- P6-02 FFT 频域滤波 / 去条纹 ---
        self.spin_fft_band = QSpinBox()
        self.spin_fft_band.setRange(1, 999)
        self.spin_fft_band.setValue(1)
        self.lbl_fft_band = QLabel("FFT 处理波段 (fftBand，1-based):")
        self.params_form.addRow(self.lbl_fft_band, self.spin_fft_band)

        self.txt_fft_suppress = QLineEdit()
        self.txt_fft_suppress.setPlaceholderText(
            "留空=恒等往返；每对 [fy,fx] 用分号分隔，例：0,4;0,-4（自动补厄米对称伙伴）"
        )
        self.lbl_fft_suppress = QLabel("抑制频率 bin (suppressBins):")
        self.params_form.addRow(self.lbl_fft_suppress, self.txt_fft_suppress)

        # --- P6-03 PCA 正/逆变换 ---
        self.cmb_pca_mode = QComboBox()
        for mode, label in (
            ("forward", "正变换 forward（输入多波段 → 主成分）"),
            ("inverse", "逆变换 inverse（主成分 + 变换文件 → 重建）"),
        ):
            self.cmb_pca_mode.addItem(label, mode)
        self.lbl_pca_mode = QLabel("PCA 模式 (pcaMode):")
        self.params_form.addRow(self.lbl_pca_mode, self.cmb_pca_mode)

        self.spin_pca_components = QSpinBox()
        self.spin_pca_components.setRange(0, 999)
        self.spin_pca_components.setSpecialValueText("全部波段")
        self.spin_pca_components.setValue(0)
        self.lbl_pca_components = QLabel("主成分数 (pcaComponents，0=全部):")
        self.params_form.addRow(self.lbl_pca_components, self.spin_pca_components)

        self.cmb_pca_matrix = QComboBox()
        self.cmb_pca_matrix.addItem("协方差矩阵 (covariance)", "covariance")
        self.cmb_pca_matrix.addItem("相关矩阵 (correlation)", "correlation")
        self.lbl_pca_matrix = QLabel("PCA 矩阵类型 (pcaMatrix):")
        self.params_form.addRow(self.lbl_pca_matrix, self.cmb_pca_matrix)

        self.txt_pca_transform = QLineEdit()
        self.txt_pca_transform.setPlaceholderText(
            "仅 inverse 需要：选择正变换写出的 <成果名>.pca.json"
        )
        self.btn_browse_pca_transform = QPushButton("选择...")
        self.btn_browse_pca_transform.clicked.connect(self._browse_pca_transform)
        pca_transform_row = QHBoxLayout()
        pca_transform_row.addWidget(self.txt_pca_transform, stretch=1)
        pca_transform_row.addWidget(self.btn_browse_pca_transform)
        self.lbl_pca_transform = QLabel("PCA 变换文件 (pcaTransform):")
        self.params_form.addRow(self.lbl_pca_transform, pca_transform_row)

        # --- P6-03 缨帽变换 ---
        self.cmb_tc_sensor = QComboBox()
        for key in sensor_keys():
            self.cmb_tc_sensor.addItem(get_sensor(key).label, key)
        self.cmb_tc_sensor.currentIndexChanged.connect(self._on_tc_sensor_changed)
        self.lbl_tc_sensor = QLabel("缨帽传感器 (tcSensor):")
        self.params_form.addRow(self.lbl_tc_sensor, self.cmb_tc_sensor)

        self.txt_tc_bands = QLineEdit()
        self.txt_tc_bands.setPlaceholderText(
            "必填 6 个输入波段号，顺序 Blue,Green,Red,NIR,SWIR1,SWIR2；例：2,3,4,5,6,7"
        )
        self.lbl_tc_bands = QLabel("输入波段 (tcBands，6 个):")
        self.params_form.addRow(self.lbl_tc_bands, self.txt_tc_bands)

        self.lbl_tc_source_title = QLabel("系数来源与产品级别:")
        self.lbl_tc_source = QLabel("")
        self.lbl_tc_source.setWordWrap(True)
        self.lbl_tc_source.setStyleSheet("color: #666; font-size: 11px;")
        self.params_form.addRow(self.lbl_tc_source_title, self.lbl_tc_source)
        self._on_tc_sensor_changed()

        # --- P6-04 GCP 配准 ---
        self.cmb_gcp_transform = QComboBox()
        for key in GCP_TRANSFORMS:
            self.cmb_gcp_transform.addItem(
                "仿射 affine（1 阶）" if key == "affine" else "二次多项式 polynomial2",
                key,
            )
        self.lbl_gcp_transform = QLabel("GCP 变换 (gcpTransform):")
        self.params_form.addRow(self.lbl_gcp_transform, self.cmb_gcp_transform)

        self.txt_gcp_file = QLineEdit()
        self.txt_gcp_file.setPlaceholderText("控制点 JSON（control/check 点，含 pixel/line/x/y）")
        self.btn_browse_gcp_file = QPushButton("选择...")
        self.btn_browse_gcp_file.clicked.connect(self._browse_gcp_file)
        gcp_row = QHBoxLayout()
        gcp_row.addWidget(self.txt_gcp_file, stretch=1)
        gcp_row.addWidget(self.btn_browse_gcp_file)
        self.lbl_gcp_file = QLabel("GCP 文件 (gcpFile):")
        self.params_form.addRow(self.lbl_gcp_file, gcp_row)

        self.txt_gcp_reference = QLineEdit()
        self.txt_gcp_reference.setPlaceholderText("参考影像（决定输出网格/CRS）")
        self.btn_browse_gcp_reference = QPushButton("选择...")
        self.btn_browse_gcp_reference.clicked.connect(self._browse_gcp_reference)
        gcp_ref_row = QHBoxLayout()
        gcp_ref_row.addWidget(self.txt_gcp_reference, stretch=1)
        gcp_ref_row.addWidget(self.btn_browse_gcp_reference)
        self.lbl_gcp_reference = QLabel("参考影像 (referenceFile):")
        self.params_form.addRow(self.lbl_gcp_reference, gcp_ref_row)

        self.cmb_gcp_resample = QComboBox()
        for alg in LOCAL_RESAMPLES:
            self.cmb_gcp_resample.addItem(alg, alg)
        self.lbl_gcp_resample = QLabel("GCP 重采样 (gcpResample):")
        self.params_form.addRow(self.lbl_gcp_resample, self.cmb_gcp_resample)

        # --- P6-04 栅格差值 ---
        self.cmb_diff_mode = QComboBox()
        for mode in DIFF_MODES:
            self.cmb_diff_mode.addItem(mode, mode)
        self.lbl_diff_mode = QLabel("差值模式 (diffMode):")
        self.params_form.addRow(self.lbl_diff_mode, self.cmb_diff_mode)

        self.txt_diff_reference = QLineEdit()
        self.txt_diff_reference.setPlaceholderText("参考影像 B（必须与输入同网格）")
        self.btn_browse_diff_reference = QPushButton("选择...")
        self.btn_browse_diff_reference.clicked.connect(self._browse_diff_reference)
        diff_ref_row = QHBoxLayout()
        diff_ref_row.addWidget(self.txt_diff_reference, stretch=1)
        diff_ref_row.addWidget(self.btn_browse_diff_reference)
        self.lbl_diff_reference = QLabel("参考影像 B (referenceFile):")
        self.params_form.addRow(self.lbl_diff_reference, diff_ref_row)

        # --- P6-04 Brovey/HSV 融合 ---
        self.txt_fusion_ms = QLineEdit()
        self.txt_fusion_ms.setPlaceholderText("多光谱影像文件")
        self.btn_browse_fusion_ms = QPushButton("选择...")
        self.btn_browse_fusion_ms.clicked.connect(self._browse_fusion_ms)
        fusion_ms_row = QHBoxLayout()
        fusion_ms_row.addWidget(self.txt_fusion_ms, stretch=1)
        fusion_ms_row.addWidget(self.btn_browse_fusion_ms)
        self.lbl_fusion_ms = QLabel("多光谱影像 (msFile):")
        self.params_form.addRow(self.lbl_fusion_ms, fusion_ms_row)

        self.txt_fusion_ms_bands = QLineEdit()
        self.txt_fusion_ms_bands.setPlaceholderText("3 个 MS 波段号（R,G,B），例：1,2,3")
        self.lbl_fusion_ms_bands = QLabel("MS 波段 (msBands，3 个):")
        self.params_form.addRow(self.lbl_fusion_ms_bands, self.txt_fusion_ms_bands)

        self.cmb_fusion_method = QComboBox()
        for method in FUSION_METHODS:
            self.cmb_fusion_method.addItem(method, method)
        self.lbl_fusion_method = QLabel("融合方法 (fusionMethod):")
        self.params_form.addRow(self.lbl_fusion_method, self.cmb_fusion_method)

        self.cmb_fusion_resample = QComboBox()
        for alg in LOCAL_RESAMPLES:
            self.cmb_fusion_resample.addItem(alg, alg)
        self.lbl_fusion_resample = QLabel("MS 重采样 (fusionResample):")
        self.params_form.addRow(self.lbl_fusion_resample, self.cmb_fusion_resample)

        # --- P6-05 二值掩膜 ---
        self.spin_mask_band = QSpinBox()
        self.spin_mask_band.setRange(1, 999)
        self.spin_mask_band.setValue(1)
        self.lbl_mask_band = QLabel("输入波段 (maskBand，1-based):")
        self.params_form.addRow(self.lbl_mask_band, self.spin_mask_band)

        self.txt_mask_low = QLineEdit()
        self.txt_mask_low.setPlaceholderText("留空=不设下限；0.2 表示 value >= 0.2")
        self.lbl_mask_low = QLabel("掩膜下限 (maskLow，可空):")
        self.params_form.addRow(self.lbl_mask_low, self.txt_mask_low)

        self.txt_mask_high = QLineEdit()
        self.txt_mask_high.setPlaceholderText("留空=不设上限；0.9 表示 value <= 0.9")
        self.lbl_mask_high = QLabel("掩膜上限 (maskHigh，可空):")
        self.params_form.addRow(self.lbl_mask_high, self.txt_mask_high)

        self.chk_mask_invert = QCheckBox("取反掩膜 (maskInvert)")
        self.chk_mask_invert.setChecked(False)
        self.lbl_mask_invert = QLabel("取反 (maskInvert):")
        self.params_form.addRow(self.lbl_mask_invert, self.chk_mask_invert)

        # --- P6-05 FVC 植被覆盖度 ---
        self.spin_fvc_band = QSpinBox()
        self.spin_fvc_band.setRange(1, 999)
        self.spin_fvc_band.setValue(1)
        self.lbl_fvc_band = QLabel("NDVI 波段 (fvcBand，1-based):")
        self.params_form.addRow(self.lbl_fvc_band, self.spin_fvc_band)

        self.cmb_fvc_mode = QComboBox()
        for mode in FVC_ENDMEMBER_MODES:
            self.cmb_fvc_mode.addItem(
                "分位端元 percentile（2%/98%）" if mode == "percentile" else "显式端元 values",
                mode,
            )
        self.lbl_fvc_mode = QLabel("端元模式 (endmemberMode):")
        self.params_form.addRow(self.lbl_fvc_mode, self.cmb_fvc_mode)

        self.spin_fvc_low = QDoubleSpinBox()
        self.spin_fvc_low.setDecimals(4)
        self.spin_fvc_low.setRange(0.0, 100.0)
        self.spin_fvc_low.setValue(2.0)
        self.lbl_fvc_low = QLabel("低分位 (lowPercentile，%):")
        self.params_form.addRow(self.lbl_fvc_low, self.spin_fvc_low)

        self.spin_fvc_high = QDoubleSpinBox()
        self.spin_fvc_high.setDecimals(4)
        self.spin_fvc_high.setRange(0.0, 100.0)
        self.spin_fvc_high.setValue(98.0)
        self.lbl_fvc_high = QLabel("高分位 (highPercentile，%):")
        self.params_form.addRow(self.lbl_fvc_high, self.spin_fvc_high)

        self.txt_fvc_soil = QLineEdit()
        self.txt_fvc_soil.setPlaceholderText("仅 values 模式必填；例：0.15")
        self.lbl_fvc_soil = QLabel("土壤端元 (fvcSoil，可空):")
        self.params_form.addRow(self.lbl_fvc_soil, self.txt_fvc_soil)

        self.txt_fvc_veg = QLineEdit()
        self.txt_fvc_veg.setPlaceholderText("仅 values 模式必填；例：0.75")
        self.lbl_fvc_veg = QLabel("植被端元 (fvcVeg，可空):")
        self.params_form.addRow(self.lbl_fvc_veg, self.txt_fvc_veg)

        self.txt_fvc_class_edges = QLineEdit()
        self.txt_fvc_class_edges.setPlaceholderText(
            "留空=缺省 0.25,0.5,0.75（4 级）；填写严格递增，例：0.3,0.6"
        )
        self.lbl_fvc_class_edges = QLabel("分级边界 (fvcClassEdges):")
        self.params_form.addRow(self.lbl_fvc_class_edges, self.txt_fvc_class_edges)

        self.txt_fvc_mask = QLineEdit()
        self.txt_fvc_mask.setPlaceholderText("可选：单波段掩膜（1=有效，0=无效；可由 gdal:mask 生成）")
        self.btn_browse_fvc_mask = QPushButton("选择...")
        self.btn_browse_fvc_mask.clicked.connect(self._browse_fvc_mask)
        fvc_mask_row = QHBoxLayout()
        fvc_mask_row.addWidget(self.txt_fvc_mask, stretch=1)
        fvc_mask_row.addWidget(self.btn_browse_fvc_mask)
        self.lbl_fvc_mask = QLabel("掩膜文件 (maskFile，可空):")
        self.params_form.addRow(self.lbl_fvc_mask, fvc_mask_row)

        self.chk_fvc_crop = QCheckBox("夹到 [0,1]（越界标记始终写入 sidecar）")
        self.chk_fvc_crop.setChecked(True)
        self.lbl_fvc_crop = QLabel("裁剪 (crop):")
        self.params_form.addRow(self.lbl_fvc_crop, self.chk_fvc_crop)

        # --- P6-06 辐射定标 ---
        self.cmb_calibrate_mode = QComboBox()
        self.cmb_calibrate_mode.addItem("线性 gain/offset (physical)", "physical")
        self.cmb_calibrate_mode.addItem("TOA 反射率 (toa_reflectance)", "toa_reflectance")
        self.lbl_calibrate_mode = QLabel("定标模式 (calibrateMode):")
        self.params_form.addRow(self.lbl_calibrate_mode, self.cmb_calibrate_mode)

        self.txt_calibrate_gains = QLineEdit()
        self.txt_calibrate_gains.setPlaceholderText("物理模式必填；逗号分隔每波段 gain，例：1,0.5")
        self.lbl_calibrate_gains = QLabel("每波段 gain (calibrateGains):")
        self.params_form.addRow(self.lbl_calibrate_gains, self.txt_calibrate_gains)

        self.txt_calibrate_offsets = QLineEdit()
        self.txt_calibrate_offsets.setPlaceholderText("可选；逗号分隔每波段 offset，例：0,-1")
        self.lbl_calibrate_offsets = QLabel("每波段 offset (calibrateOffsets):")
        self.params_form.addRow(self.lbl_calibrate_offsets, self.txt_calibrate_offsets)

        self.txt_calibrate_esun = QLineEdit()
        self.txt_calibrate_esun.setPlaceholderText("TOA 反射率模式必填；逗号分隔每波段 ESUN")
        self.lbl_calibrate_esun = QLabel("每波段 ESUN (calibrateEsun):")
        self.params_form.addRow(self.lbl_calibrate_esun, self.txt_calibrate_esun)

        self.spin_calibrate_sun_zen = QDoubleSpinBox()
        self.spin_calibrate_sun_zen.setDecimals(6)
        self.spin_calibrate_sun_zen.setRange(0.0, 180.0)
        self.spin_calibrate_sun_zen.setValue(0.0)
        self.spin_calibrate_sun_zen.setSpecialValueText("未指定")
        self.lbl_calibrate_sun_zen = QLabel("太阳天顶角 (sunZenAngle，°):")
        self.params_form.addRow(self.lbl_calibrate_sun_zen, self.spin_calibrate_sun_zen)

        # --- P6-06 DOS1 大气校正 ---
        self.spin_dos_band = QSpinBox()
        self.spin_dos_band.setRange(1, 999)
        self.spin_dos_band.setValue(1)
        self.lbl_dos_band = QLabel("输入波段 (dosBand，1-based):")
        self.params_form.addRow(self.lbl_dos_band, self.spin_dos_band)

        self.txt_haze_dn = QLineEdit()
        self.txt_haze_dn.setPlaceholderText("必填；暗对象 DN，例：1.0")
        self.lbl_haze_dn = QLabel("暗对象 DN (hazeDN):")
        self.params_form.addRow(self.lbl_haze_dn, self.txt_haze_dn)

        self.txt_dos_esun = QLineEdit()
        self.txt_dos_esun.setPlaceholderText("必填；该波段 ESUN，例：2000.0")
        self.lbl_dos_esun = QLabel("波段 ESUN (dosEsun):")
        self.params_form.addRow(self.lbl_dos_esun, self.txt_dos_esun)

        self.txt_dos_sun_zen = QLineEdit()
        self.txt_dos_sun_zen.setPlaceholderText("必填；太阳天顶角（°），例：45.0")
        self.lbl_dos_sun_zen = QLabel("太阳天顶角 (sunZenAngle，°):")
        self.params_form.addRow(self.lbl_dos_sun_zen, self.txt_dos_sun_zen)

        self.txt_dos_gain = QLineEdit()
        self.txt_dos_gain.setPlaceholderText("可选；线性 gain，缺省 1.0")
        self.lbl_dos_gain = QLabel("线性 gain (dosGain):")
        self.params_form.addRow(self.lbl_dos_gain, self.txt_dos_gain)

        self.txt_dos_offset = QLineEdit()
        self.txt_dos_offset.setPlaceholderText("可选；线性 offset，缺省 0.0")
        self.lbl_dos_offset = QLabel("线性 offset (dosOffset):")
        self.params_form.addRow(self.lbl_dos_offset, self.txt_dos_offset)

        self.txt_dos_dist = QLineEdit()
        self.txt_dos_dist.setPlaceholderText("可选；日地距离 d，缺省 1.0")
        self.lbl_dos_dist = QLabel("日地距离 (earthSunDistance):")
        self.params_form.addRow(self.lbl_dos_dist, self.txt_dos_dist)

        # --- P6-06 多时相去云 ---
        self.txt_cloud_secondary = QLineEdit()
        self.txt_cloud_secondary.setPlaceholderText("二期（云下源）影像文件")
        self.btn_browse_cloud_secondary = QPushButton("选择...")
        self.btn_browse_cloud_secondary.clicked.connect(self._browse_cloud_secondary)
        cloud_secondary_row = QHBoxLayout()
        cloud_secondary_row.addWidget(self.txt_cloud_secondary, stretch=1)
        cloud_secondary_row.addWidget(self.btn_browse_cloud_secondary)
        self.lbl_cloud_secondary = QLabel("二期影像 (secondaryFile):")
        self.params_form.addRow(self.lbl_cloud_secondary, cloud_secondary_row)

        self.txt_cloud_mask = QLineEdit()
        self.txt_cloud_mask.setPlaceholderText("云掩膜（单波段，1=云）")
        self.btn_browse_cloud_mask = QPushButton("选择...")
        self.btn_browse_cloud_mask.clicked.connect(self._browse_cloud_mask)
        cloud_mask_row = QHBoxLayout()
        cloud_mask_row.addWidget(self.txt_cloud_mask, stretch=1)
        cloud_mask_row.addWidget(self.btn_browse_cloud_mask)
        self.lbl_cloud_mask = QLabel("云掩膜 (cloudMaskFile):")
        self.params_form.addRow(self.lbl_cloud_mask, cloud_mask_row)

        self.txt_cloud_band_list = QLineEdit()
        self.txt_cloud_band_list.setPlaceholderText("可选；参与替换的波段，例：1,2,3（空=全部）")
        self.lbl_cloud_band_list = QLabel("波段 (bandList，可空):")
        self.params_form.addRow(self.lbl_cloud_band_list, self.txt_cloud_band_list)

        self.txt_cloud_primary_time = QLineEdit()
        self.txt_cloud_primary_time.setPlaceholderText("可选；一期时间戳，写入 sidecar")
        self.lbl_cloud_primary_time = QLabel("一期时间 (primaryTime，可空):")
        self.params_form.addRow(self.lbl_cloud_primary_time, self.txt_cloud_primary_time)

        self.txt_cloud_secondary_time = QLineEdit()
        self.txt_cloud_secondary_time.setPlaceholderText("可选；二期时间戳，写入 sidecar")
        self.lbl_cloud_secondary_time = QLabel("二期时间 (secondaryTime，可空):")
        self.params_form.addRow(self.lbl_cloud_secondary_time, self.txt_cloud_secondary_time)

        self.spin_cloud_dilate = QSpinBox()
        self.spin_cloud_dilate.setRange(0, 999)
        self.spin_cloud_dilate.setValue(0)
        self.lbl_cloud_dilate = QLabel("掩膜膨胀像素 (maskDilatePixels):")
        self.params_form.addRow(self.lbl_cloud_dilate, self.spin_cloud_dilate)

        # --- P8-01 多维数组二维切片 ---
        self.txt_mdim_array = QLineEdit()
        self.txt_mdim_array.setPlaceholderText("多维数组名（例：temp / sst；点工具箱探测多维树后填入）")
        self.lbl_mdim_array = QLabel("数组 (array):")
        self.params_form.addRow(self.lbl_mdim_array, self.txt_mdim_array)

        self.txt_mdim_select = QLineEdit()
        self.txt_mdim_select.setPlaceholderText(
            "钉死维度（dim=index，除两保留维外全部；例：time=1,depth=0）"
        )
        self.lbl_mdim_select = QLabel("钉死维 (dimSelect):")
        self.params_form.addRow(self.lbl_mdim_select, self.txt_mdim_select)

        # --- P4-03 概览构建 ---
        self.txt_levels = QLineEdit()
        self.txt_levels.setPlaceholderText("严格递增、每个≥2；例：2,4,8")
        self.lbl_levels = QLabel("概览倍率 (levels):")
        self.params_form.addRow(self.lbl_levels, self.txt_levels)

        self.cmb_ovr_compress = QComboBox()
        self.cmb_ovr_compress.addItem("不压缩 (NONE)", "")
        self.cmb_ovr_compress.addItem("DEFLATE", "DEFLATE")
        self.cmb_ovr_compress.addItem("LZW", "LZW")
        self.lbl_ovr_compress = QLabel("概览压缩 (overviewCompress):")
        self.params_form.addRow(self.lbl_ovr_compress, self.cmb_ovr_compress)

        # --- P4-04 表达式计算 / 统计报告 ---
        self.txt_calc = QLineEdit()
        self.txt_calc.setPlaceholderText(
            "例：(A-B)/(A+B)；波段按输入顺序 A/B/C…，支持 + - * / // % ** 与白名单函数"
        )
        self.lbl_calc = QLabel("受限表达式 (calc):")
        self.params_form.addRow(self.lbl_calc, self.txt_calc)

        self.cmb_stats_mode = QComboBox()
        self.cmb_stats_mode.addItem("抽样（sampled，标注为估计）", "sampled")
        self.cmb_stats_mode.addItem("全量（full，超限拒绝）", "full")
        self.lbl_stats_mode = QLabel("统计模式 (statisticsMode):")
        self.params_form.addRow(self.lbl_stats_mode, self.cmb_stats_mode)

        self.spin_hist_bins = QSpinBox()
        self.spin_hist_bins.setRange(2, 4096)
        self.spin_hist_bins.setValue(256)
        self.lbl_hist_bins = QLabel("直方图分箱数 (histogramBins):")
        self.params_form.addRow(self.lbl_hist_bins, self.spin_hist_bins)

        self.spin_max_samples = QSpinBox()
        self.spin_max_samples.setRange(1, 1_000_000_000)
        self.spin_max_samples.setValue(1_000_000)
        self.lbl_max_samples = QLabel("抽样预算 (maxSamples):")
        self.params_form.addRow(self.lbl_max_samples, self.spin_max_samples)

        self.spin_full_limit = QSpinBox()
        self.spin_full_limit.setRange(1, 1_000_000_000)
        self.spin_full_limit.setValue(50_000_000)
        self.lbl_full_limit = QLabel("全量像元上限 (fullLimit):")
        self.params_form.addRow(self.lbl_full_limit, self.spin_full_limit)

        # --- P4-01 裁剪（像元窗口 xoff,yoff,xsize,ysize）---
        self.txt_clip = QLineEdit()
        self.txt_clip.setPlaceholderText("留空表示不裁剪；例：10,20,100,80")
        self.lbl_clip = QLabel("裁剪窗口 (像元 xoff,yoff,xsize,ysize):")
        self.params_form.addRow(self.lbl_clip, self.txt_clip)

        # --- P4-01 输出像元尺寸 ---
        self.spin_xres = QDoubleSpinBox()
        self.spin_xres.setDecimals(8)
        # 0 是「不指定」哨兵（specialValueText 在最小值显示）；
        # 组装参数时只有 value() > 0 才会传给引擎，0 永不下发。
        self.spin_xres.setRange(0.0, 1e12)
        self.spin_xres.setSpecialValueText("不指定")
        self.spin_xres.setValue(0.0)
        self.lbl_xres = QLabel("输出像元宽 (xRes):")
        self.params_form.addRow(self.lbl_xres, self.spin_xres)

        self.spin_yres = QDoubleSpinBox()
        self.spin_yres.setDecimals(8)
        self.spin_yres.setRange(0.0, 1e12)
        self.spin_yres.setSpecialValueText("不指定")
        self.spin_yres.setValue(0.0)
        self.lbl_yres = QLabel("输出像元高 (yRes):")
        self.params_form.addRow(self.lbl_yres, self.spin_yres)

        # --- P4-01 输出尺寸 ---
        self.txt_outsize = QLineEdit()
        self.txt_outsize.setPlaceholderText("留空表示不指定；例：512,512")
        self.lbl_outsize = QLabel("输出尺寸 (width,height):")
        self.params_form.addRow(self.lbl_outsize, self.txt_outsize)

        # --- P4-01 输出网格对齐（仅 Warp）---
        self.chk_tap = QCheckBox("输出网格对齐到像元边界 (targetAlignedPixels)")
        self.chk_tap.setChecked(False)
        self.lbl_tap = QLabel("输出网格:")
        self.params_form.addRow(self.lbl_tap, self.chk_tap)

        # --- 数据类型 ---
        self.cmb_out_type = QComboBox()
        self.cmb_out_type.addItems(["保持原样 (Default)", "Byte (uint8)", "UInt16", "Int16", "Float32", "Float64"])
        self.lbl_out_type = QLabel("输出数据类型:")
        self.params_form.addRow(self.lbl_out_type, self.cmb_out_type)

        # --- 目标坐标系 (Warp) ---
        self.cmb_dst_crs = QComboBox()
        self.cmb_dst_crs.setEditable(True)
        self.cmb_dst_crs.addItems([
            "EPSG:3857",
            "EPSG:4326",
            "EPSG:32650",
            "EPSG:32649",
            "EPSG:32651",
        ])
        self.lbl_dst_crs = QLabel("目标坐标系 (Dst SRS):")
        self.params_form.addRow(self.lbl_dst_crs, self.cmb_dst_crs)

        # --- 重采样方法 (Warp) ---
        self.cmb_resampling = QComboBox()
        # 只展示契约模块声明的**实际支持**的算法，且直接用 canonical 名做 itemData，
        # 避免「显示文本」与「Worker 识别的名字」不一致导致静默回退。
        for canonical, label in RESAMPLE_ALGORITHM_CHOICES:
            self.cmb_resampling.addItem(label, canonical)
        self.lbl_resampling = QLabel("重采样方法:")
        self.params_form.addRow(self.lbl_resampling, self.cmb_resampling)

        # --- 创建选项 ---
        self.txt_co = QLineEdit()
        self.txt_co.setPlaceholderText("例如: TILED=YES, COMPRESS=DEFLATE")
        self.lbl_co = QLabel("创建选项 (Creation Options):")
        self.params_form.addRow(self.lbl_co, self.txt_co)

        self.form_layout.addWidget(self.grp_params)
        self.form_layout.addStretch()

        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, stretch=1)

        # 4. 底部执行提交按钮
        self.btn_submit = QPushButton("🚀 提交执行任务 (Run Task)")
        self.btn_submit.setStyleSheet(
            "QPushButton { background-color: #27ae60; color: white; font-weight: bold; "
            "font-size: 13px; padding: 10px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #2ecc71; } "
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self.btn_submit.clicked.connect(self._on_submit_clicked)
        self.btn_submit.setEnabled(False)
        main_layout.addWidget(self.btn_submit)

    def set_project_root(self, root: Path | str) -> None:
        self._project_root = Path(root).resolve()

    def set_metadata_provider(
        self, provider: Callable[[Path], dict[str, Any]] | None
    ) -> None:
        """注入 GUI 侧只读元数据提供者（通常由 MainWindow 调用 worker probe）。"""
        self._metadata_provider = provider

    def set_input_path(self, path: Path | str) -> None:
        """从图层列表自动填充输入路径，并智能推荐默认输出路径。"""
        p = Path(path).resolve()
        self.txt_input.setText(str(p))
        if not self.txt_output.text():
            # 矢量成果（P5-02）默认 GeoPackage 容器，避免推荐 .tif 后缀误导
            suffix = ".gpkg" if self._is_vector_cap() else ".tif"
            out_name = f"{p.stem}_processed{suffix}"
            self.txt_output.setText(str(p.parent / out_name))

    def _is_vector_cap(self) -> bool:
        cap = self._current_cap
        return cap is not None and is_vector_output(cap.capability_id)

    def _apply_format_choices(self, capability_id: str) -> None:
        """按能力族重填输出格式下拉（矢量成果只有 GPKG / GeoJSON）。"""
        items = _VECTOR_FORMAT_ITEMS if is_vector_output(capability_id) else _RASTER_FORMAT_ITEMS
        current_items = [self.cmb_format.itemText(i) for i in range(self.cmb_format.count())]
        if current_items == items:
            return
        self.cmb_format.blockSignals(True)
        self.cmb_format.clear()
        self.cmb_format.addItems(items)
        self.cmb_format.blockSignals(False)

    def load_capability(self, cap: CapabilityDescriptor) -> None:
        """载入并呈现一项能力的参数配置表单或未支持说明。"""
        self._current_cap = cap

        # 1. 标题与徽标
        self.lbl_title.setText(f"{cap.name} ({cap.capability_id})")
        prov_badge = f"[{cap.engine.provider_id.upper()}]"
        kind_badge = f"[{cap.kind.value}]"
        avail_badge = f"[{cap.state.availability.value}]"
        self.lbl_badges.setText(f"{prov_badge} {kind_badge} 状态: {avail_badge}")
        self.lbl_desc.setText(cap.description or "无详细说明")

        # 2. 判断是否属于已接通能力
        is_connected = cap.capability_id in CONNECTED_CAPABILITIES

        if not is_connected:
            self.grp_params.setEnabled(False)
            self.lbl_unsupported_banner.setVisible(True)
            reasons = "; ".join(cap.limitations) if cap.limitations else "平台适配器当前尚未接入该算法执行通道。"
            self.lbl_unsupported_banner.setText(
                f"⚠️ 未接入平台执行器（禁止提交）：\n"
                f"该能力在当前基准中已编目，但未桥接至后台工作线程。\n"
                f"限制说明: {reasons}"
            )
            self.btn_submit.setEnabled(False)
            self.btn_submit.setText("🚫 暂未接入平台，禁止提交")
            return

        # 3. 已接通能力配置
        self.grp_params.setEnabled(True)
        self.lbl_unsupported_banner.setVisible(False)
        self.btn_submit.setEnabled(True)
        self.btn_submit.setText("🚀 提交执行任务 (Run Task)")

        cid = cap.capability_id
        is_warp = "warp" in cid
        is_translate = "translate" in cid
        is_rasterio = "rasterio" in cid
        is_rasterize = cid == "gdal:rasterize"
        is_grid = cid == "gdal:grid"
        sup = supported_params_for(cid) or frozenset()

        # 显示或隐藏相应字段（统计报告输出 JSON artifact，不显示栅格格式选项）
        show_format = (
            is_translate
            or is_warp
            or is_rasterio
            or "calc" in sup
            or supports_output_format(cid)
        )
        self.lbl_format.setVisible(show_format)
        self.cmb_format.setVisible(show_format)
        # 矢量成果（P5-02）的格式候选项与栅格不同，按能力族重填
        self._apply_format_choices(cid)

        show_out_type = is_translate or is_rasterize or is_grid or "outputType" in sup
        self.lbl_out_type.setVisible(show_out_type)
        self.cmb_out_type.setVisible(show_out_type)

        self.lbl_co.setVisible(is_translate)
        self.txt_co.setVisible(is_translate)

        self.lbl_dst_crs.setVisible(is_warp)
        self.cmb_dst_crs.setVisible(is_warp)

        self.lbl_resampling.setVisible(is_warp)
        self.cmb_resampling.setVisible(is_warp)

        # 输入区：多输入能力用「可排序列表」，单输入能力用单文件行
        multi = is_multi_input(cid)
        self._set_row_visible(self.lbl_input, self.txt_input, not multi)
        self.btn_browse_in.setVisible(not multi)
        self.grp_inputs.setVisible(multi)

        # P4-02 / P4-04 专属参数：按共享契约的支持矩阵显隐
        self._set_row_visible(
            self.lbl_overlap_priority, self.cmb_overlap_priority, "overlapPriority" in sup
        )
        self._set_row_visible(self.lbl_resolution, self.cmb_resolution, "resolution" in sup)
        self._set_row_visible(self.lbl_src_nodata, self.txt_src_nodata, "srcNodata" in sup)
        self._set_row_visible(self.lbl_vrt_nodata, self.txt_vrt_nodata, "vrtNodata" in sup)
        self._set_row_visible(self.lbl_dst_nodata, self.txt_dst_nodata, "dstNodata" in sup)
        self._set_row_visible(self.lbl_separate, self.chk_separate, "separate" in sup)
        self._set_row_visible(
            self.lbl_relative_paths, self.chk_relative_paths, "relativePaths" in sup
        )
        self._set_row_visible(self.lbl_band_list, self.txt_band_list, "bandList" in sup)
        # P5-02 地形分析
        self._set_row_visible(
            self.lbl_dem_processing, self.cmb_dem_processing, "processing" in sup
        )
        self._set_row_visible(self.lbl_dem_zfactor, self.spin_dem_zfactor, "zFactor" in sup)
        self._set_row_visible(self.lbl_dem_scale, self.spin_dem_scale, "scale" in sup)
        # P5-02 矢量栅格化
        self._set_row_visible(
            self.lbl_burn_values, self.txt_burn_values, "burnValues" in sup
        )
        self._set_row_visible(self.lbl_attribute, self.txt_attribute, "attribute" in sup)
        self._set_row_visible(
            self.lbl_all_touched, self.chk_all_touched, "allTouched" in sup
        )
        # P5-02 散点栅格插值
        self._set_row_visible(
            self.lbl_grid_algorithm, self.cmb_grid_algorithm, "algorithm" in sup
        )
        self._set_row_visible(self.lbl_zfield, self.txt_zfield, "zField" in sup)
        # P5-02 栅格矢量化（成果为矢量）
        self._set_row_visible(
            self.lbl_field_name, self.txt_field_name, "fieldName" in sup
        )
        self._set_row_visible(
            self.lbl_connectedness, self.cmb_connectedness, "connectedness" in sup
        )
        # P5-02 矢量化输入波段（polygonize/contour 共用）
        self._set_row_visible(self.lbl_band, self.spin_band, "band" in sup)
        # P5-02 等值线（成果为矢量）
        self._set_row_visible(
            self.lbl_contour_interval, self.spin_contour_interval, "interval" in sup
        )
        self._set_row_visible(
            self.lbl_contour_offset, self.spin_contour_offset, "offset" in sup
        )
        self._set_row_visible(
            self.lbl_fixed_levels, self.txt_fixed_levels, "fixedLevels" in sup
        )
        # P5-04 矢量处理 / SQL / 坐标转换
        for label, widget, key in (
            (self.lbl_vt_sql, self.txt_vt_sql, "sql"),
            (self.lbl_vt_where, self.txt_vt_where, "where"),
            (self.lbl_vt_spat, self.txt_vt_spat, "spat"),
            (self.lbl_vt_select, self.txt_vt_select, "select"),
            (self.lbl_vt_dst_srs, self.txt_vt_dst_srs, "dstSRS"),
        ):
            self._set_row_visible(label, widget, key in sup)
        # P6-01 光谱指数
        self._set_row_visible(self.lbl_sindex, self.cmb_sindex, "sindex" in sup)
        self._set_row_visible(self.lbl_band_a, self.spin_band_a, "bandA" in sup)
        self._set_row_visible(self.lbl_band_b, self.spin_band_b, "bandB" in sup)
        self._set_row_visible(self.lbl_savi_l, self.spin_savi_l, "saviL" in sup)
        self._set_row_visible(
            self.lbl_use_physical, self.chk_use_physical, "usePhysical" in sup
        )
        # P6-02 FFT 滤波 / 去条纹
        self._set_row_visible(
            self.lbl_fft_band, self.spin_fft_band, "fftBand" in sup
        )
        self._set_row_visible(
            self.lbl_fft_suppress, self.txt_fft_suppress, "suppressBins" in sup
        )
        # P6-03 PCA 正/逆变换
        self._set_row_visible(self.lbl_pca_mode, self.cmb_pca_mode, "pcaMode" in sup)
        self._set_row_visible(
            self.lbl_pca_components, self.spin_pca_components, "pcaComponents" in sup
        )
        self._set_row_visible(
            self.lbl_pca_matrix, self.cmb_pca_matrix, "pcaMatrix" in sup
        )
        self._set_row_visible(
            self.lbl_pca_transform, self.txt_pca_transform, "pcaTransform" in sup
        )
        self.btn_browse_pca_transform.setVisible("pcaTransform" in sup)
        # P6-03 缨帽变换
        self._set_row_visible(self.lbl_tc_sensor, self.cmb_tc_sensor, "tcSensor" in sup)
        self._set_row_visible(self.lbl_tc_bands, self.txt_tc_bands, "tcBands" in sup)
        self._set_row_visible(
            self.lbl_tc_source_title, self.lbl_tc_source, "tcSensor" in sup
        )
        # P6-04 GCP 配准
        self._set_row_visible(
            self.lbl_gcp_transform, self.cmb_gcp_transform, "gcpTransform" in sup
        )
        self._set_row_visible(self.lbl_gcp_file, self.txt_gcp_file, "gcpFile" in sup)
        self.btn_browse_gcp_file.setVisible("gcpFile" in sup)
        self._set_row_visible(
            self.lbl_gcp_reference, self.txt_gcp_reference, "referenceFile" in sup and "gcpFile" in sup
        )
        self.btn_browse_gcp_reference.setVisible(
            "referenceFile" in sup and "gcpFile" in sup
        )
        self._set_row_visible(
            self.lbl_gcp_resample, self.cmb_gcp_resample, "gcpResample" in sup
        )
        # P6-04 栅格差值
        self._set_row_visible(
            self.lbl_diff_mode, self.cmb_diff_mode, "diffMode" in sup
        )
        self._set_row_visible(
            self.lbl_diff_reference, self.txt_diff_reference, "referenceFile" in sup and "diffMode" in sup
        )
        self.btn_browse_diff_reference.setVisible(
            "referenceFile" in sup and "diffMode" in sup
        )
        # P6-04 融合
        self._set_row_visible(self.lbl_fusion_ms, self.txt_fusion_ms, "msFile" in sup)
        self.btn_browse_fusion_ms.setVisible("msFile" in sup)
        self._set_row_visible(
            self.lbl_fusion_ms_bands, self.txt_fusion_ms_bands, "msBands" in sup
        )
        self._set_row_visible(
            self.lbl_fusion_method, self.cmb_fusion_method, "fusionMethod" in sup
        )
        self._set_row_visible(
            self.lbl_fusion_resample, self.cmb_fusion_resample, "fusionResample" in sup
        )
        # P6-05 二值掩膜
        self._set_row_visible(
            self.lbl_mask_band, self.spin_mask_band, "maskBand" in sup
        )
        self._set_row_visible(self.lbl_mask_low, self.txt_mask_low, "maskLow" in sup)
        self._set_row_visible(self.lbl_mask_high, self.txt_mask_high, "maskHigh" in sup)
        self._set_row_visible(
            self.lbl_mask_invert, self.chk_mask_invert, "maskInvert" in sup
        )
        # P6-05 FVC
        self._set_row_visible(self.lbl_fvc_band, self.spin_fvc_band, "fvcBand" in sup)
        self._set_row_visible(
            self.lbl_fvc_mode, self.cmb_fvc_mode, "endmemberMode" in sup
        )
        self._set_row_visible(self.lbl_fvc_low, self.spin_fvc_low, "lowPercentile" in sup)
        self._set_row_visible(
            self.lbl_fvc_high, self.spin_fvc_high, "highPercentile" in sup
        )
        self._set_row_visible(self.lbl_fvc_soil, self.txt_fvc_soil, "fvcSoil" in sup)
        self._set_row_visible(self.lbl_fvc_veg, self.txt_fvc_veg, "fvcVeg" in sup)
        self._set_row_visible(
            self.lbl_fvc_class_edges, self.txt_fvc_class_edges, "fvcClassEdges" in sup
        )
        self._set_row_visible(self.lbl_fvc_mask, self.txt_fvc_mask, "maskFile" in sup)
        self.btn_browse_fvc_mask.setVisible("maskFile" in sup)
        self._set_row_visible(self.lbl_fvc_crop, self.chk_fvc_crop, "crop" in sup)
        # P6-06 辐射定标
        self._set_row_visible(
            self.lbl_calibrate_mode, self.cmb_calibrate_mode, "calibrateMode" in sup
        )
        self._set_row_visible(
            self.lbl_calibrate_gains, self.txt_calibrate_gains, "calibrateGains" in sup
        )
        self._set_row_visible(
            self.lbl_calibrate_offsets, self.txt_calibrate_offsets, "calibrateOffsets" in sup
        )
        self._set_row_visible(
            self.lbl_calibrate_esun, self.txt_calibrate_esun, "calibrateEsun" in sup
        )
        self._set_row_visible(
            self.lbl_calibrate_sun_zen, self.spin_calibrate_sun_zen, "sunZenAngle" in sup
        )
        # P6-06 DOS1
        self._set_row_visible(self.lbl_dos_band, self.spin_dos_band, "dosBand" in sup)
        self._set_row_visible(self.lbl_haze_dn, self.txt_haze_dn, "hazeDN" in sup)
        self._set_row_visible(self.lbl_dos_esun, self.txt_dos_esun, "dosEsun" in sup)
        self._set_row_visible(
            self.lbl_dos_sun_zen, self.txt_dos_sun_zen, "sunZenAngle" in sup
        )
        self._set_row_visible(self.lbl_dos_gain, self.txt_dos_gain, "dosGain" in sup)
        self._set_row_visible(self.lbl_dos_offset, self.txt_dos_offset, "dosOffset" in sup)
        self._set_row_visible(
            self.lbl_dos_dist, self.txt_dos_dist, "earthSunDistance" in sup
        )
        # P6-06 去云
        self._set_row_visible(
            self.lbl_cloud_secondary, self.txt_cloud_secondary, "secondaryFile" in sup
        )
        self.btn_browse_cloud_secondary.setVisible("secondaryFile" in sup)
        self._set_row_visible(
            self.lbl_cloud_mask, self.txt_cloud_mask, "cloudMaskFile" in sup
        )
        self.btn_browse_cloud_mask.setVisible("cloudMaskFile" in sup)
        self._set_row_visible(
            self.lbl_cloud_band_list, self.txt_cloud_band_list, "bandList" in sup
        )
        self._set_row_visible(
            self.lbl_cloud_primary_time, self.txt_cloud_primary_time, "primaryTime" in sup
        )
        self._set_row_visible(
            self.lbl_cloud_secondary_time,
            self.txt_cloud_secondary_time,
            "secondaryTime" in sup,
        )
        self._set_row_visible(
            self.lbl_cloud_dilate, self.spin_cloud_dilate, "maskDilatePixels" in sup
        )
        self._set_row_visible(self.lbl_levels, self.txt_levels, "levels" in sup)
        self._set_row_visible(self.lbl_ovr_compress, self.cmb_ovr_compress, "overviewCompress" in sup)
        # P8-01 多维切片
        self._set_row_visible(self.lbl_mdim_array, self.txt_mdim_array, "array" in sup)
        self._set_row_visible(
            self.lbl_mdim_select, self.txt_mdim_select, "dimSelect" in sup
        )
        # P4-04
        self._set_row_visible(self.lbl_calc, self.txt_calc, "calc" in sup)
        self._set_row_visible(self.lbl_stats_mode, self.cmb_stats_mode, "statisticsMode" in sup)
        self._set_row_visible(self.lbl_hist_bins, self.spin_hist_bins, "histogramBins" in sup)
        self._set_row_visible(self.lbl_max_samples, self.spin_max_samples, "maxSamples" in sup)
        self._set_row_visible(self.lbl_full_limit, self.spin_full_limit, "fullLimit" in sup)

        # P4-01：裁剪 / 重采样 / 输出网格
        self._set_row_visible(self.lbl_clip, self.txt_clip, is_translate or is_warp)
        self._set_row_visible(self.lbl_xres, self.spin_xres, is_translate or is_warp or is_rasterize)
        self._set_row_visible(self.lbl_yres, self.spin_yres, is_translate or is_warp or is_rasterize)
        self._set_row_visible(self.lbl_outsize, self.txt_outsize, is_translate or is_warp or is_rasterize or is_grid)
        self._set_row_visible(self.lbl_tap, self.chk_tap, is_warp)

        # 切换能力时**隔离状态**：把新能力不适用的控件值重置。
        # 仅隐藏控件是不够的——残留值仍会进入 TaskSpec（复核实测：
        # 切到 Rasterio convert 后隐藏的 srcWin/xRes/yRes 仍被提交）。
        self._reset_inapplicable_grid_state(cid)

    # ------------------------------------------------------------------
    # 多输入列表操作（P4-02）
    # ------------------------------------------------------------------
    def _add_input_files(self) -> None:
        """向多输入列表追加文件（可多选）。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择输入影像（可多选）", str(self._project_root), "栅格影像 (*.tif *.tiff *.img *.dat *.vrt);;全部文件 (*)"
        )
        for path in paths:
            self.lst_inputs.addItem(path)

    def _remove_selected_input(self) -> None:
        row = self.lst_inputs.currentRow()
        if row >= 0:
            self.lst_inputs.takeItem(row)

    def _move_selected_input(self, delta: int) -> None:
        """上下移动选中项——顺序即输入顺序，决定重叠优先级。"""
        row = self.lst_inputs.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.lst_inputs.count():
            return
        item = self.lst_inputs.takeItem(row)
        self.lst_inputs.insertItem(target, item)
        self.lst_inputs.setCurrentRow(target)

    def input_paths(self) -> list[Path]:
        """当前多输入列表里的路径（按显示顺序）。"""
        return [Path(self.lst_inputs.item(i).text()) for i in range(self.lst_inputs.count())]

    def _reset_inapplicable_grid_state(self, capability_id: str) -> None:
        """重置当前能力**不适用**的裁剪/网格控件值。

        适用性以共享契约的支持矩阵为准（与 Worker 同一份），
        避免 GUI 与 Worker 各维护一套能力边界。
        """
        supported = supported_params_for(capability_id)
        if supported is None:
            # 未知能力：不猜测边界，清空全部网格类控件（宁可少参数，不可错参数）
            supported = frozenset()

        if "srcWin" not in supported:
            self.txt_clip.clear()
        if "xRes" not in supported:
            self.spin_xres.setValue(0.0)
        if "yRes" not in supported:
            self.spin_yres.setValue(0.0)
        if "width" not in supported:
            self.txt_outsize.clear()
        if "targetAlignedPixels" not in supported:
            self.chk_tap.setChecked(False)

        # P4-02 专属控件的状态隔离（隐藏控件的值不得进入 TaskSpec）
        if "overlapPriority" not in supported:
            self.cmb_overlap_priority.setCurrentIndex(0)
        if "resolution" not in supported:
            self.cmb_resolution.setCurrentIndex(0)
        if "srcNodata" not in supported:
            self.txt_src_nodata.clear()
        if "vrtNodata" not in supported:
            self.txt_vrt_nodata.clear()
        if "dstNodata" not in supported:
            self.txt_dst_nodata.clear()
        if "separate" not in supported:
            self.chk_separate.setChecked(False)
        if "relativePaths" not in supported:
            self.chk_relative_paths.setChecked(True)
        if "bandList" not in supported:
            self.txt_band_list.clear()
        if "processing" not in supported:
            self.cmb_dem_processing.setCurrentIndex(0)
        if "zFactor" not in supported:
            self.spin_dem_zfactor.setValue(1.0)
        if "scale" not in supported:
            self.spin_dem_scale.setValue(1.0)
        if "burnValues" not in supported:
            self.txt_burn_values.clear()
        if "attribute" not in supported:
            self.txt_attribute.clear()
        if "allTouched" not in supported:
            self.chk_all_touched.setChecked(False)
        if "algorithm" not in supported:
            self.cmb_grid_algorithm.setCurrentIndex(0)
        if "zField" not in supported:
            self.txt_zfield.clear()
        if "fieldName" not in supported:
            self.txt_field_name.setText("DN")
        if "connectedness" not in supported:
            self.cmb_connectedness.setCurrentIndex(0)
        if "band" not in supported:
            self.spin_band.setValue(1)
        if "interval" not in supported:
            self.spin_contour_interval.setValue(10.0)
        if "offset" not in supported:
            self.spin_contour_offset.setValue(0.0)
        if "fixedLevels" not in supported:
            self.txt_fixed_levels.clear()
        for key, widget in (
            ("sql", self.txt_vt_sql),
            ("where", self.txt_vt_where),
            ("spat", self.txt_vt_spat),
            ("select", self.txt_vt_select),
            ("dstSRS", self.txt_vt_dst_srs),
        ):
            if key not in supported:
                widget.clear()
        if "sindex" not in supported:
            self.cmb_sindex.setCurrentIndex(0)
        if "bandA" not in supported:
            self.spin_band_a.setValue(1)
        if "bandB" not in supported:
            self.spin_band_b.setValue(2)
        if "saviL" not in supported:
            self.spin_savi_l.setValue(0.5)
        if "usePhysical" not in supported:
            self.chk_use_physical.setChecked(True)
        if "fftBand" not in supported:
            self.spin_fft_band.setValue(1)
        if "suppressBins" not in supported:
            self.txt_fft_suppress.clear()
        if "pcaMode" not in supported:
            self.cmb_pca_mode.setCurrentIndex(0)
        if "pcaComponents" not in supported:
            self.spin_pca_components.setValue(0)
        if "pcaMatrix" not in supported:
            self.cmb_pca_matrix.setCurrentIndex(0)
        if "pcaTransform" not in supported:
            self.txt_pca_transform.clear()
        if "tcSensor" not in supported:
            self.cmb_tc_sensor.setCurrentIndex(0)
        if "tcBands" not in supported:
            self.txt_tc_bands.clear()
        if "gcpTransform" not in supported:
            self.cmb_gcp_transform.setCurrentIndex(0)
        if "gcpFile" not in supported:
            self.txt_gcp_file.clear()
        if "referenceFile" not in supported:
            self.txt_gcp_reference.clear()
            self.txt_diff_reference.clear()
        if "gcpResample" not in supported:
            self.cmb_gcp_resample.setCurrentIndex(0)
        if "diffMode" not in supported:
            self.cmb_diff_mode.setCurrentIndex(0)
        if "msFile" not in supported:
            self.txt_fusion_ms.clear()
        if "msBands" not in supported:
            self.txt_fusion_ms_bands.clear()
        if "fusionMethod" not in supported:
            self.cmb_fusion_method.setCurrentIndex(0)
        if "fusionResample" not in supported:
            self.cmb_fusion_resample.setCurrentIndex(0)
        if "maskBand" not in supported:
            self.spin_mask_band.setValue(1)
        if "maskLow" not in supported:
            self.txt_mask_low.clear()
        if "maskHigh" not in supported:
            self.txt_mask_high.clear()
        if "maskInvert" not in supported:
            self.chk_mask_invert.setChecked(False)
        if "fvcBand" not in supported:
            self.spin_fvc_band.setValue(1)
        if "endmemberMode" not in supported:
            self.cmb_fvc_mode.setCurrentIndex(0)
        if "lowPercentile" not in supported:
            self.spin_fvc_low.setValue(2.0)
        if "highPercentile" not in supported:
            self.spin_fvc_high.setValue(98.0)
        if "fvcSoil" not in supported:
            self.txt_fvc_soil.clear()
        if "fvcVeg" not in supported:
            self.txt_fvc_veg.clear()
        if "fvcClassEdges" not in supported:
            self.txt_fvc_class_edges.clear()
        if "maskFile" not in supported:
            self.txt_fvc_mask.clear()
        if "crop" not in supported:
            self.chk_fvc_crop.setChecked(True)
        if "calibrateMode" not in supported:
            self.cmb_calibrate_mode.setCurrentIndex(0)
        if "calibrateGains" not in supported:
            self.txt_calibrate_gains.clear()
        if "calibrateOffsets" not in supported:
            self.txt_calibrate_offsets.clear()
        if "calibrateEsun" not in supported:
            self.txt_calibrate_esun.clear()
        if "sunZenAngle" not in supported:
            self.spin_calibrate_sun_zen.setValue(0.0)
        if "dosBand" not in supported:
            self.spin_dos_band.setValue(1)
        if "hazeDN" not in supported:
            self.txt_haze_dn.clear()
        if "dosEsun" not in supported:
            self.txt_dos_esun.clear()
        if "dosGain" not in supported:
            self.txt_dos_gain.clear()
        if "dosOffset" not in supported:
            self.txt_dos_offset.clear()
        if "earthSunDistance" not in supported:
            self.txt_dos_dist.clear()
        if "secondaryFile" not in supported:
            self.txt_cloud_secondary.clear()
        if "cloudMaskFile" not in supported:
            self.txt_cloud_mask.clear()
        if "bandList" not in supported:
            self.txt_cloud_band_list.clear()
        if "primaryTime" not in supported:
            self.txt_cloud_primary_time.clear()
        if "secondaryTime" not in supported:
            self.txt_cloud_secondary_time.clear()
        if "maskDilatePixels" not in supported:
            self.spin_cloud_dilate.setValue(0)
        if "array" not in supported:
            self.txt_mdim_array.clear()
        if "dimSelect" not in supported:
            self.txt_mdim_select.clear()
        if "levels" not in supported:
            self.txt_levels.clear()
        if "overviewCompress" not in supported:
            self.cmb_ovr_compress.setCurrentIndex(0)
        # P4-04 控件的状态隔离
        if "calc" not in supported:
            self.txt_calc.clear()
        if "statisticsMode" not in supported:
            self.cmb_stats_mode.setCurrentIndex(0)
            self.spin_hist_bins.setValue(256)
            self.spin_max_samples.setValue(1_000_000)
            self.spin_full_limit.setValue(50_000_000)

    @staticmethod
    def _set_row_visible(label: QLabel, widget: QWidget, visible: bool) -> None:
        """同时显隐 QFormLayout 里的标签与控件。"""
        label.setVisible(visible)
        widget.setVisible(visible)

    def _collect_p402_params(self, capability_id: str) -> tuple[dict[str, Any], str | None]:
        """采集 P4-02 专属参数（只采集当前能力适用的控件）。

        返回 (参数字典, 错误消息)。

        NoData 语义关键：**留空表示未设置**（不产生参数），
        **填 0 表示设置为 0**（必须产生参数）——
        二者不可混淆，否则 dstNodata=0 会被当成未设置丢掉，
        缺口就不会被重映射（第 29 轮复核的实际缺陷）。
        """
        params: dict[str, Any] = {}
        supported = supported_params_for(capability_id) or frozenset()

        if "overlapPriority" in supported:
            params["overlapPriority"] = self.cmb_overlap_priority.currentData()

        if "resolution" in supported:
            res = self.cmb_resolution.currentData()
            if res:
                params["resolution"] = res

        for widget, key in (
            (self.txt_src_nodata, "srcNodata"),
            (self.txt_vrt_nodata, "vrtNodata"),
            (self.txt_dst_nodata, "dstNodata"),
        ):
            if key not in supported:
                continue
            raw = widget.text().strip()
            if not raw:
                continue  # 留空 = 未设置
            try:
                params[key] = float(raw)
            except ValueError:
                return params, f"{key} 必须是数字（留空表示不设置），当前为：{raw}"

        if "separate" in supported:
            params["separate"] = self.chk_separate.isChecked()

        if "relativePaths" in supported:
            params["relativePaths"] = self.chk_relative_paths.isChecked()

        if "levels" in supported:
            raw_levels = self.txt_levels.text().strip()
            if not raw_levels:
                return params, "概览倍率 levels 必填（逗号分隔、严格递增、每个≥2，例：2,4,8）"
            parts_lv = [x.strip() for x in raw_levels.replace(";", ",").split(",") if x.strip()]
            lv_out: list[int] = []
            for part in parts_lv:
                try:
                    lv_out.append(int(part))
                except ValueError:
                    return params, f"概览倍率需要逗号分隔的整数，当前为：{raw_levels}"
            params["levels"] = lv_out

        if "overviewCompress" in supported:
            comp = self.cmb_ovr_compress.currentData()
            if comp:
                params["overviewCompress"] = comp

        if "bandList" in supported:
            raw_bands = self.txt_band_list.text().strip()
            if raw_bands:
                parts = [x.strip() for x in raw_bands.replace(";", ",").split(",") if x.strip()]
                bands: list[int] = []
                for part in parts:
                    try:
                        bands.append(int(part))
                    except ValueError:
                        return params, f"波段顺序需要逗号分隔的整数，当前为：{raw_bands}"
                if not bands:
                    return params, "波段顺序不能为空列表（留空表示保留全部）"
                params["bandList"] = bands

        # P5-02：地形分析
        if "processing" in supported:
            params["processing"] = self.cmb_dem_processing.currentData()
        if "zFactor" in supported:
            params["zFactor"] = float(self.spin_dem_zfactor.value())
        if "scale" in supported:
            params["scale"] = float(self.spin_dem_scale.value())

        # P5-02：矢量栅格化
        if "burnValues" in supported:
            raw_burn = self.txt_burn_values.text().strip()
            if raw_burn:
                parts_burn = [
                    x.strip()
                    for x in raw_burn.replace(";", ",").split(",")
                    if x.strip()
                ]
                burns: list[float] = []
                for part in parts_burn:
                    try:
                        burns.append(float(part))
                    except ValueError:
                        return params, f"burnValues 需要逗号分隔的数字，当前为：{raw_burn}"
                if burns:
                    params["burnValues"] = burns
        if "attribute" in supported:
            attr = self.txt_attribute.text().strip()
            if attr:
                params["attribute"] = attr
        if "allTouched" in supported:
            params["allTouched"] = self.chk_all_touched.isChecked()
        if params.get("burnValues") and params.get("attribute"):
            return params, "burnValues 与 attribute 互斥，不能同时指定"

        # P5-02：散点栅格插值
        if "algorithm" in supported:
            params["algorithm"] = self.cmb_grid_algorithm.currentData()
        if "zField" in supported:
            zfield = self.txt_zfield.text().strip()
            if zfield:
                params["zField"] = zfield

        # P5-02：栅格矢量化（成果为矢量）
        if "fieldName" in supported:
            field_name = self.txt_field_name.text().strip()
            if field_name:
                params["fieldName"] = field_name
        if "connectedness" in supported:
            conn = self.cmb_connectedness.currentData()
            if conn:
                params["connectedness"] = int(conn)
        if "band" in supported and self.spin_band.value() != 1:
            # 1 是 Worker 侧默认值，不产生冗余参数；显式选择其它波段才下发
            params["band"] = int(self.spin_band.value())

        # P5-02：等值线（成果为矢量）
        if "interval" in supported and self.spin_contour_interval.value() > 0:
            params["interval"] = float(self.spin_contour_interval.value())
        # 否则不产生参数，由 Worker 以 invalid_parameters 明确拒绝缺步长
        if "offset" in supported and self.spin_contour_offset.value() != 0.0:
            # 0 是 Worker 侧默认值，不产生冗余参数；非 0 基准高程如实下发
            params["offset"] = float(self.spin_contour_offset.value())
        if "fixedLevels" in supported:
            raw_fixed = self.txt_fixed_levels.text().strip()
            if raw_fixed:
                parts_fixed = [
                    x.strip() for x in raw_fixed.replace(";", ",").split(",") if x.strip()
                ]
                fixed_out: list[float] = []
                for part in parts_fixed:
                    try:
                        fixed_out.append(float(part))
                    except ValueError:
                        return params, f"fixedLevels 需要逗号分隔的高程数值，当前为：{raw_fixed}"
                if fixed_out:
                    params["fixedLevels"] = fixed_out

        # P5-04：矢量处理 / SQL / 坐标转换
        if "sql" in supported:
            sql_text = self.txt_vt_sql.text().strip()
            if sql_text:
                params["sql"] = sql_text
        if "where" in supported:
            where_text = self.txt_vt_where.text().strip()
            if where_text:
                params["where"] = where_text
        if "spat" in supported:
            raw_spat = self.txt_vt_spat.text().strip()
            if raw_spat:
                parts_spat = [
                    x.strip() for x in raw_spat.replace(";", ",").split(",") if x.strip()
                ]
                if len(parts_spat) != 4:
                    return params, f"spat 需要逗号分隔的 minx,miny,maxx,maxy，当前为：{raw_spat}"
                try:
                    params["spat"] = [float(p) for p in parts_spat]
                except ValueError:
                    return params, f"spat 需要数值，当前为：{raw_spat}"
        if "select" in supported:
            raw_select = self.txt_vt_select.text().strip()
            if raw_select:
                fields_out = [
                    x.strip() for x in raw_select.replace(";", ",").split(",") if x.strip()
                ]
                if fields_out:
                    params["select"] = fields_out
        if "dstSRS" in supported:
            dst_srs_text = self.txt_vt_dst_srs.text().strip()
            if dst_srs_text:
                params["dstSRS"] = dst_srs_text

        # P6-01：光谱指数
        if "sindex" in supported:
            params["sindex"] = self.cmb_sindex.currentData()
        if "bandA" in supported:
            params["bandA"] = int(self.spin_band_a.value())
        if "bandB" in supported:
            params["bandB"] = int(self.spin_band_b.value())
        if "saviL" in supported and self.spin_savi_l.value() != 0.5:
            # 0.5 是 Worker 侧默认值，不产生冗余参数
            params["saviL"] = float(self.spin_savi_l.value())
        if "usePhysical" in supported and not self.chk_use_physical.isChecked():
            # True 是 Worker 侧默认值；关闭物理量换算才显式下发
            params["usePhysical"] = False

        # P6-02：FFT 频域滤波 / 去条纹
        if "fftBand" in supported:
            params["fftBand"] = int(self.spin_fft_band.value())
        if "suppressBins" in supported:
            raw_bins = self.txt_fft_suppress.text().strip()
            if raw_bins:
                pairs: list[list[int]] = []
                # 每对 [fy,fx] 用分号分隔；逗号只作分量分隔符。
                for chunk in raw_bins.replace("；", ";").split(";"):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    parts_bin = [
                        x.strip() for x in chunk.replace("，", ",").split(",") if x.strip()
                    ]
                    if len(parts_bin) != 2:
                        return params, (
                            "suppressBins 每对必须是 [fy,fx] 两个整数，"
                            f"当前片段为：{chunk}"
                        )
                    try:
                        fy, fx = int(parts_bin[0]), int(parts_bin[1])
                    except ValueError:
                        return params, (
                            "suppressBins 的 fy/fx 必须是整数（不接受小数截断），"
                            f"当前片段为：{chunk}"
                        )
                    pairs.append([fy, fx])
                if pairs:
                    params["suppressBins"] = pairs

        # P6-03：PCA 正/逆变换
        if "pcaMode" in supported:
            mode = str(self.cmb_pca_mode.currentData() or "forward")
            params["pcaMode"] = mode
            if "pcaComponents" in supported and self.spin_pca_components.value() > 0:
                params["pcaComponents"] = int(self.spin_pca_components.value())
            if mode == "forward":
                if "pcaMatrix" in supported:
                    params["pcaMatrix"] = self.cmb_pca_matrix.currentData()
            else:
                transform = self.txt_pca_transform.text().strip()
                if not transform:
                    return params, "PCA inverse 模式必须选择正变换写出的 .pca.json 变换文件"
                params["pcaTransform"] = transform

        # P6-03：缨帽变换
        if "tcSensor" in supported:
            params["tcSensor"] = self.cmb_tc_sensor.currentData()
        if "tcBands" in supported:
            raw_tc = self.txt_tc_bands.text().strip()
            if not raw_tc:
                return params, (
                    "缨帽变换必须给出 6 个输入波段号（按 Blue,Green,Red,NIR,SWIR1,SWIR2 顺序）"
                )
            parts_tc = [
                x.strip()
                for x in raw_tc.replace(";", ",").replace("；", ",").split(",")
                if x.strip()
            ]
            if len(parts_tc) != 6:
                return params, f"tcBands 必须恰好 6 个波段号，当前为：{raw_tc}"
            try:
                bands_tc = [int(part) for part in parts_tc]
            except ValueError:
                return params, f"tcBands 必须是整数波段号，当前为：{raw_tc}"
            params["tcBands"] = bands_tc

        # P6-04：GCP 配准
        if "gcpTransform" in supported:
            params["gcpTransform"] = self.cmb_gcp_transform.currentData()
        if "gcpFile" in supported:
            gcp_text = self.txt_gcp_file.text().strip()
            if not gcp_text:
                return params, "GCP 配准必须选择控制点 JSON 文件"
            params["gcpFile"] = gcp_text
            reference_text = self.txt_gcp_reference.text().strip()
            if not reference_text:
                return params, "GCP 配准必须选择参考影像"
            params["referenceFile"] = reference_text
        if "gcpResample" in supported:
            params["gcpResample"] = self.cmb_gcp_resample.currentData()

        # P6-04：栅格差值
        if "diffMode" in supported:
            params["diffMode"] = self.cmb_diff_mode.currentData()
            reference_text = self.txt_diff_reference.text().strip()
            if not reference_text:
                return params, "栅格差值必须选择参考影像 B"
            params["referenceFile"] = reference_text

        # P6-04：Brovey/HSV 融合
        if "msFile" in supported:
            ms_text = self.txt_fusion_ms.text().strip()
            if not ms_text:
                return params, "影像融合必须选择多光谱影像文件"
            params["msFile"] = ms_text
        if "msBands" in supported:
            raw_ms = self.txt_fusion_ms_bands.text().strip()
            if not raw_ms:
                return params, "影像融合必须给出 3 个多光谱波段号（R,G,B）"
            parts_ms = [
                x.strip()
                for x in raw_ms.replace(";", ",").replace("；", ",").split(",")
                if x.strip()
            ]
            if len(parts_ms) != 3:
                return params, f"msBands 必须恰好 3 个波段号，当前为：{raw_ms}"
            try:
                params["msBands"] = [int(part) for part in parts_ms]
            except ValueError:
                return params, f"msBands 必须是整数波段号，当前为：{raw_ms}"
        if "fusionMethod" in supported:
            params["fusionMethod"] = self.cmb_fusion_method.currentData()
        if "fusionResample" in supported:
            params["fusionResample"] = self.cmb_fusion_resample.currentData()

        # P6-05：二值掩膜
        if "maskBand" in supported:
            params["maskBand"] = int(self.spin_mask_band.value())
        for key, widget in (
            ("maskLow", self.txt_mask_low),
            ("maskHigh", self.txt_mask_high),
        ):
            if key in supported:
                raw = widget.text().strip()
                if raw:
                    try:
                        params[key] = float(raw)
                    except ValueError:
                        return params, f"{key} 必须是数值，当前为：{raw}"
        if "maskInvert" in supported:
            params["maskInvert"] = self.chk_mask_invert.isChecked()

        # P6-05：FVC
        if "fvcBand" in supported:
            params["fvcBand"] = int(self.spin_fvc_band.value())
        if "endmemberMode" in supported:
            params["endmemberMode"] = self.cmb_fvc_mode.currentData()
        if "lowPercentile" in supported:
            params["lowPercentile"] = float(self.spin_fvc_low.value())
        if "highPercentile" in supported:
            params["highPercentile"] = float(self.spin_fvc_high.value())
        if "fvcSoil" in supported:
            raw_soil = self.txt_fvc_soil.text().strip()
            if raw_soil:
                try:
                    params["fvcSoil"] = float(raw_soil)
                except ValueError:
                    return params, f"fvcSoil 必须是数值，当前为：{raw_soil}"
        if "fvcVeg" in supported:
            raw_veg = self.txt_fvc_veg.text().strip()
            if raw_veg:
                try:
                    params["fvcVeg"] = float(raw_veg)
                except ValueError:
                    return params, f"fvcVeg 必须是数值，当前为：{raw_veg}"
        if "fvcClassEdges" in supported:
            raw_edges = self.txt_fvc_class_edges.text().strip()
            if raw_edge := raw_edges:
                edge_values: list[float] = []
                for part in raw_edge.replace(";", ",").replace("；", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        edge_values.append(float(part))
                    except ValueError:
                        return params, f"fvcClassEdges 必须是数值，当前为：{raw_edges}"
                if edge_values:
                    params["fvcClassEdges"] = edge_values
        if "maskFile" in supported:
            mask_text = self.txt_fvc_mask.text().strip()
            if mask_text:
                params["maskFile"] = mask_text
        if "crop" in supported:
            params["crop"] = self.chk_fvc_crop.isChecked()

        # P6-06：辐射定标
        if "calibrateMode" in supported:
            params["calibrateMode"] = self.cmb_calibrate_mode.currentData()
        if "calibrateGains" in supported:
            raw_gains = self.txt_calibrate_gains.text().strip()
            if raw_gains:
                gains: list[float] = []
                for part in raw_gains.replace(";", ",").replace("；", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        gains.append(float(part))
                    except ValueError:
                        return params, f"calibrateGains 必须是数值，当前为：{raw_gains}"
                if gains:
                    params["calibrateGains"] = gains
        if "calibrateOffsets" in supported:
            raw_offsets = self.txt_calibrate_offsets.text().strip()
            if raw_offsets:
                offsets: list[float] = []
                for part in raw_offsets.replace(";", ",").replace("；", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        offsets.append(float(part))
                    except ValueError:
                        return params, f"calibrateOffsets 必须是数值，当前为：{raw_offsets}"
                if offsets:
                    params["calibrateOffsets"] = offsets
        if "calibrateEsun" in supported:
            raw_esun = self.txt_calibrate_esun.text().strip()
            if raw_esun:
                esun_values: list[float] = []
                for part in raw_esun.replace(";", ",").replace("；", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        esun_values.append(float(part))
                    except ValueError:
                        return params, f"calibrateEsun 必须是数值，当前为：{raw_esun}"
                if esun_values:
                    params["calibrateEsun"] = esun_values
        if "sunZenAngle" in supported and self.spin_calibrate_sun_zen.value() > 0.0:
            params["sunZenAngle"] = float(self.spin_calibrate_sun_zen.value())

        # P6-06：DOS1
        if "dosBand" in supported:
            params["dosBand"] = int(self.spin_dos_band.value())
        for key, widget in (
            ("hazeDN", self.txt_haze_dn),
            ("dosEsun", self.txt_dos_esun),
            ("sunZenAngle", self.txt_dos_sun_zen),
            ("dosGain", self.txt_dos_gain),
            ("dosOffset", self.txt_dos_offset),
            ("earthSunDistance", self.txt_dos_dist),
        ):
            if key not in supported:
                continue
            raw = widget.text().strip()
            if raw:
                try:
                    params[key] = float(raw)
                except ValueError:
                    return params, f"{key} 必须是数值，当前为：{raw}"

        # P6-06：多时相去云
        if "secondaryFile" in supported:
            secondary_text = self.txt_cloud_secondary.text().strip()
            if not secondary_text:
                return params, "多时相去云必须选择二期（云下源）影像"
            params["secondaryFile"] = secondary_text
        if "cloudMaskFile" in supported:
            cloud_mask_text = self.txt_cloud_mask.text().strip()
            if not cloud_mask_text:
                return params, "多时相去云必须选择云掩膜文件"
            params["cloudMaskFile"] = cloud_mask_text
        if "bandList" in supported:
            raw_cloud_bands = self.txt_cloud_band_list.text().strip()
            if raw_cloud_bands:
                cloud_band_values: list[int] = []
                for part in raw_cloud_bands.replace(";", ",").replace("；", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        cloud_band_values.append(int(part))
                    except ValueError:
                        return params, f"bandList 必须是整数波段号，当前为：{raw_cloud_bands}"
                if cloud_band_values:
                    params["bandList"] = cloud_band_values
        if "primaryTime" in supported:
            raw_time = self.txt_cloud_primary_time.text().strip()
            if raw_time:
                params["primaryTime"] = raw_time
        if "secondaryTime" in supported:
            raw_time = self.txt_cloud_secondary_time.text().strip()
            if raw_time:
                params["secondaryTime"] = raw_time
        if "maskDilatePixels" in supported:
            params["maskDilatePixels"] = int(self.spin_cloud_dilate.value())

        # P8-01：多维切片
        if "array" in supported:
            arr_text = self.txt_mdim_array.text().strip()
            if not arr_text:
                return params, "多维切片必须填数组名 array（例：temp / sst）"
            params["array"] = arr_text
        if "dimSelect" in supported:
            sel_text = self.txt_mdim_select.text().strip()
            if not sel_text:
                return params, "多维切片必须填 dimSelect（dim=index，除两保留维外全部）"
            params["dimSelect"] = sel_text

        # P4-04：表达式计算
        if "calc" in supported:
            raw_expr = self.txt_calc.text().strip()
            if not raw_expr:
                return params, "受限表达式 calc 必填（例：(A-B)/(A+B)）"
            params["calc"] = raw_expr
            if capability_id == "gdal:calc":
                raw_type = self.cmb_out_type.currentText()
                if "Default" not in raw_type:
                    params["outputType"] = raw_type.split()[0]

        # P4-04：统计报告（模式/分箱/抽样预算/全量上限）
        if "statisticsMode" in supported:
            params["statisticsMode"] = self.cmb_stats_mode.currentData()
            params["histogramBins"] = self.spin_hist_bins.value()
            params["maxSamples"] = self.spin_max_samples.value()
            params["fullLimit"] = self.spin_full_limit.value()

        return params, None

    def _collect_grid_params(
        self,
        is_warp: bool,
        capability_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """采集 P4-01 的裁剪 / 重采样 / 输出网格参数。

        返回 (参数字典, 错误消息)。错误消息非 None 表示参数非法，调用方应提示并中止提交。
        空值一律不产生参数（绝不传 0 或空串给引擎）。

        只采集**当前能力适用**的控件：能力不消费某类参数时，其控件值一律不采集，
        即使控件里还有上一能力残留的值（仅隐藏控件不足以隔离能力状态）。
        """
        params: dict[str, Any] = {}

        supported = supported_params_for(capability_id)
        if supported is None:
            # 未知能力：按「不采集任何网格类参数」处理（宁可少参数，不可错参数）
            supported = frozenset()

        clip_text = self.txt_clip.text().strip()
        if clip_text and "srcWin" in supported:
            parts = [x.strip() for x in clip_text.replace(";", ",").split(",") if x.strip()]
            if len(parts) != 4:
                return params, "裁剪窗口需要 4 个整数（xoff, yoff, xsize, ysize），例如：10,20,100,80"
            try:
                clip_vals = [int(x) for x in parts]
            except ValueError:
                return params, "裁剪窗口必须是整数，例如：10,20,100,80"
            if clip_vals[2] <= 0 or clip_vals[3] <= 0:
                return params, "裁剪窗口的宽高必须为正数"
            params["srcWin"] = clip_vals

        x_res = (
            float(self.spin_xres.value())
            if "xRes" in supported and self.spin_xres.value() > 0
            else None
        )
        y_res = (
            float(self.spin_yres.value())
            if "yRes" in supported and self.spin_yres.value() > 0
            else None
        )
        if x_res is not None:
            params["xRes"] = x_res
        if y_res is not None:
            params["yRes"] = y_res

        outsize_text = self.txt_outsize.text().strip() if "width" in supported else ""
        if outsize_text:
            parts = [x.strip() for x in outsize_text.replace(";", ",").split(",") if x.strip()]
            if len(parts) != 2:
                return params, "输出尺寸需要 2 个整数（width, height）"
            try:
                out_w, out_h = int(parts[0]), int(parts[1])
            except ValueError:
                return params, "输出尺寸必须是整数"
            if out_w <= 0 or out_h <= 0:
                return params, "输出尺寸必须为正数"
            params["width"] = out_w
            params["height"] = out_h

        if self.chk_tap.isChecked() and is_warp and "targetAlignedPixels" in supported:
            params["targetAlignedPixels"] = True

        return params, None

    def _browse_pca_transform(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 PCA 变换文件",
            str(self._project_root),
            "PCA 变换文件 (*.json);;所有文件 (*)",
        )
        if path:
            self.txt_pca_transform.setText(path)

    def _on_tc_sensor_changed(self) -> None:
        key = self.cmb_tc_sensor.currentData()
        if key:
            self.lbl_tc_source.setText(sensor_source_line(str(key)))

    def _browse_gcp_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 GCP 控制点文件",
            str(self._project_root),
            "GCP JSON (*.json);;所有文件 (*)",
        )
        if path:
            self.txt_gcp_file.setText(path)

    def _browse_gcp_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 GCP 参考影像",
            str(self._project_root),
            "栅格影像 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_gcp_reference.setText(path)

    def _browse_diff_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择差值参考影像 B",
            str(self._project_root),
            "栅格影像 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_diff_reference.setText(path)

    def _browse_fusion_ms(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择多光谱影像",
            str(self._project_root),
            "栅格影像 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_fusion_ms.setText(path)

    def _browse_fvc_mask(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 FVC 掩膜文件",
            str(self._project_root),
            "栅格掩膜 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_fvc_mask.setText(path)

    def _browse_cloud_secondary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择一期（可能带云）影像",
            str(self._project_root),
            "栅格影像 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_cloud_secondary.setText(path)

    def _browse_cloud_mask(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择云掩膜文件",
            str(self._project_root),
            "栅格掩膜 (*.tif *.tiff *.dat *.img *.vrt);;所有文件 (*)",
        )
        if path:
            self.txt_cloud_mask.setText(path)

    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择输入数据",
            str(self._project_root),
            "栅格/矢量 (*.tif *.tiff *.dat *.img *.hdr *.png *.jpg *.geojson *.json *.gpkg *.shp);;所有文件 (*.*)",
        )
        if path:
            self.set_input_path(path)

    def _browse_output(self) -> None:
        init_dir = str(Path(self.txt_input.text()).parent) if self.txt_input.text() else str(self._project_root)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出文件",
            init_dir,
            "GeoTIFF (*.tif);;ENVI (*.dat);;GeoPackage (*.gpkg);;GeoJSON (*.geojson *.json);;所有文件 (*)",
        )
        if path:
            self.txt_output.setText(path)

    def _preflight_output_format(
        self,
        capability_id: str,
        in_paths: list[Path],
        out_fmt: str,
        params: dict[str, Any],
    ) -> str | None:
        """Use the shared format matrix before submitting a task.

        Metadata comes from the injected worker probe when available. Without
        metadata only the unambiguous parameter-level rules are enforced; the
        worker still performs the authoritative check.
        """
        if capability_id == "gdal:statistics":
            return None
        if is_vector_output(capability_id):
            # P5-02 矢量成果走 OGR 驱动矩阵（GPKG/GeoJSON），与栅格矩阵无关
            vector_violations = validate_vector_output_format(out_fmt)
            return "；".join(vector_violations) if vector_violations else None
        if capability_id == "gdal:dem":
            dem_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=1,
                has_nodata=False,
                has_alpha=False,
            )
            return "；".join(dem_violations) if dem_violations else None
        if capability_id == "gdal:rasterize":
            rasterize_violations = validate_output_format(
                out_fmt,
                dtype=str(params.get("outputType") or "Float64"),
                bands=len(params.get("burnValues") or []) or 1,
                has_nodata=params.get("noData") is not None,
                has_alpha=False,
            )
            return "；".join(rasterize_violations) if rasterize_violations else None
        if capability_id == "gdal:grid":
            grid_violations = validate_output_format(
                out_fmt,
                dtype=str(params.get("outputType") or "Float64"),
                bands=1,
                has_nodata=params.get("noData") is not None,
                has_alpha=False,
            )
            return "；".join(grid_violations) if grid_violations else None
        if capability_id in ("gdal:sindex", "gdal:fftfilter"):
            # 光谱指数 / FFT 滤波：输出恒为单波段 Float32 且必设 NoData
            violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=1,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(violations) if violations else None
        if capability_id == "gdal:pca":
            mode = str(params.get("pcaMode") or "forward")
            if mode == "inverse":
                transform_text = str(params.get("pcaTransform") or "").strip()
                if not transform_text:
                    return "PCA inverse 模式必须选择 .pca.json 变换文件"
                try:
                    payload = json.loads(
                        Path(transform_text).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    return f"无法读取 PCA 变换文件：{exc}"
                bands = int(payload.get("band_count") or 0)
                if bands <= 0:
                    return "PCA 变换文件缺少有效的 band_count"
            else:
                bands = int(params.get("pcaComponents") or 0)
                if bands <= 0 and self._metadata_provider is not None and in_paths:
                    try:
                        info = self._metadata_provider(in_paths[0])
                        bands = int(info.get("count") or len(info.get("bands") or []))
                    except Exception:
                        bands = 0
                if bands <= 0:
                    # 输入元数据不可得时不做波段级预判；worker 仍会做权威校验
                    return None
            pca_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=bands,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(pca_violations) if pca_violations else None
        if capability_id == "gdal:tasseledcap":
            tc_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=3,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(tc_violations) if tc_violations else None
        if capability_id in ("gdal:gcpregister", "gdal:rasterdiff"):
            bands = 0
            if self._metadata_provider is not None and in_paths:
                try:
                    info = self._metadata_provider(in_paths[0])
                    bands = int(info.get("count") or len(info.get("bands") or []))
                except Exception:
                    bands = 0
            if bands <= 0:
                return None
            violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=bands,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(violations) if violations else None
        if capability_id == "gdal:pansharpen":
            fusion_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=3,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(fusion_violations) if fusion_violations else None
        if capability_id == "gdal:mask":
            mask_violations = validate_output_format(
                out_fmt,
                dtype="Byte",
                bands=1,
                has_nodata=False,
                has_alpha=False,
            )
            return "；".join(mask_violations) if mask_violations else None
        if capability_id == "gdal:fvc":
            fvc_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=2,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(fvc_violations) if fvc_violations else None
        if capability_id == "gdal:calibrate":
            bands = 0
            if self._metadata_provider is not None and in_paths:
                try:
                    info = self._metadata_provider(in_paths[0])
                    bands = int(info.get("count") or len(info.get("bands") or []))
                except Exception:
                    bands = 0
            if bands <= 0:
                return None
            calibrate_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=bands,
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(calibrate_violations) if calibrate_violations else None
        if capability_id in ("gdal:dos1", "gdal:cloudreplace"):
            bands = 0
            if self._metadata_provider is not None and in_paths:
                try:
                    info = self._metadata_provider(in_paths[0])
                    bands = int(info.get("count") or len(info.get("bands") or []))
                except Exception:
                    bands = 0
            if bands <= 0:
                return None
            if capability_id == "gdal:dos1":
                bands = 1
            violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=max(bands, 1),
                has_nodata=True,
                has_alpha=False,
            )
            return "；".join(violations) if violations else None
        has_nodata_param = any(
            params.get(key) is not None
            for key in ("srcNodata", "vrtNodata", "dstNodata", "noData")
        )
        if out_fmt.upper() in {"PNG", "JPEG"} and has_nodata_param:
            return f"{out_fmt} 不支持 NoData；请改用 GTiff/COG/ENVI 或清除 NoData 参数"

        infos: list[dict[str, Any]] = []
        if self._metadata_provider is not None:
            for path in in_paths:
                try:
                    infos.append(self._metadata_provider(path))
                except Exception:
                    return None
        if not infos:
            raw_type_text = self.cmb_out_type.currentText()
            dtype = None if "Default" in raw_type_text else raw_type_text.split()[0]
            if dtype and out_fmt.upper() in {"PNG", "JPEG"}:
                bands = 1
                if capability_id == "gdal:bandmanage" and params.get("bandList"):
                    bands = len(params["bandList"])
                violations = validate_output_format(
                    out_fmt,
                    dtype=dtype,
                    bands=bands,
                    has_nodata=has_nodata_param,
                    has_alpha=False,
                )
                if violations:
                    return "；".join(violations)
            return None

        has_nodata = has_nodata_param
        has_alpha = False
        has_mask = False
        for info in infos:
            bands_info = info.get("bands") or []
            for band in bands_info:
                has_nodata = has_nodata or band.get("nodata") is not None
                has_alpha = has_alpha or bool(band.get("has_alpha"))
                flags = int(band.get("mask_flags") or 0)
                has_mask = has_mask or bool(flags & 2)
        if has_alpha:
            has_mask = False

        if capability_id == "gdal:stack":
            bands = sum(int(info.get("count") or len(info.get("bands") or [])) for info in infos)
        elif capability_id == "gdal:bandmanage":
            bands = len(params.get("bandList") or []) or int(
                infos[0].get("count") or len(infos[0].get("bands") or [])
            )
        else:
            bands = int(infos[0].get("count") or len(infos[0].get("bands") or []))

        raw_type_text = self.cmb_out_type.currentText()
        dtype = params.get("outputType")
        if not dtype:
            dtype = None if "Default" in raw_type_text else raw_type_text.split()[0]
        if not dtype:
            dtypes = infos[0].get("dtypes") or []
            dtype = dtypes[0] if dtypes else ""

        violations = validate_output_format(
            out_fmt,
            dtype=str(dtype or ""),
            bands=bands,
            has_nodata=has_nodata,
            has_alpha=has_alpha,
            has_mask=has_mask,
        )
        return "；".join(violations) if violations else None

    def _on_submit_clicked(self) -> None:
        if not self._current_cap:
            return

        cid = self._current_cap.capability_id
        multi = is_multi_input(cid)

        out_str = self.txt_output.text().strip()
        if not out_str:
            QMessageBox.warning(self, "参数不全", "请指定输出成果路径！")
            return

        in_place = cid in ("gdal:editmeta", "gdal:overview")

        # 输入：多输入能力用列表（顺序即重叠优先级依据），单输入能力用单文件行
        if multi:
            raw_inputs = [str(x) for x in self.input_paths()]
            # 表达式计算允许单源（如 A*2）；拼接类仍要求 ≥2 源。
            min_inputs = 1 if cid == "gdal:calc" else 2
            if len(raw_inputs) < min_inputs:
                hint = (
                    "输入顺序依次对应表达式里的 A/B/C… 波段名。"
                    if cid == "gdal:calc"
                    else "顺序决定重叠优先级。"
                )
                QMessageBox.warning(
                    self,
                    "输入不足",
                    f"能力 {cid} 需要至少 {min_inputs} 个输入源，当前 {len(raw_inputs)} 个。\n"
                    f"请在「多输入源」列表中继续添加（{hint}）。",
                )
                return
        else:
            in_str = self.txt_input.text().strip()
            if not in_str:
                QMessageBox.warning(self, "参数不全", "请指定输入影像路径！")
                return
            raw_inputs = [in_str]

        in_paths = [Path(x).resolve() for x in raw_inputs]
        out_p = Path(out_str).resolve()

        if in_place and (len(in_paths) != 1 or in_paths[0] != out_p):
                QMessageBox.warning(
                    self,
                    "in-place 校验",
                    "元数据编辑/概览构建为就地更新：输出路径必须与输入路径完全相同。",
                )
                return

        for in_p in in_paths:
            if not in_p.exists():
                QMessageBox.warning(self, "输入不存在", f"输入文件不存在:\n{in_p}")
                return
        if any(in_p == out_p for in_p in in_paths):
            QMessageBox.critical(self, "同源冲突", "输入路径与输出路径指向同一文件，操作已被安全拒绝！")
            return

        # 组装 TaskSpec
        overwrite = self.chk_overwrite.isChecked()
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        params: dict[str, Any] = {"overwrite": overwrite}

        raw_fmt = self.cmb_format.currentText().split()[0]
        fmt_map = {
            "GTiff": "GTiff",
            "ENVI": "ENVI",
            "COG": "COG",
            "PNG": "PNG",
            "JPEG": "JPEG",
            "GPKG": "GPKG",
            "GeoJSON": "GeoJSON",
            "Shapefile": "ESRI Shapefile",
            "FlatGeobuf": "FlatGeobuf",
        }
        out_fmt = fmt_map.get(raw_fmt, "GTiff")

        if "warp" in cid:
            params["format"] = out_fmt
            dst_crs_val = self.cmb_dst_crs.currentText().strip()
            params["dst_crs"] = dst_crs_val
            params["dst_srs"] = dst_crs_val
            # canonical 名（契约唯一来源）；此前发的是 resampling，Worker 不认，
            # 导致用户选的双线性被静默执行成最近邻。
            params["resampleAlg"] = self.cmb_resampling.currentData()
        elif "translate" in cid:
            params["format"] = out_fmt
            raw_type = self.cmb_out_type.currentText()
            if "Default" not in raw_type:
                params["output_type"] = raw_type.split()[0]
            co_text = self.txt_co.text().strip()
            if co_text:
                params["creation_options"] = [opt.strip() for opt in co_text.split(",") if opt.strip()]

        elif "rasterio" in cid:
            params["driver"] = out_fmt
        elif cid in {"gdal:merge", "gdal_merge", "gdal:stack", "gdal:bandmanage"}:
            # P4-02 组合能力的输出容器由用户选择；Worker 消费 format 参数，
            # 并在写盘前按 S4 矩阵拒绝不支持的组合。
            params["format"] = out_fmt
        elif cid == "gdal:dem":
            params["format"] = out_fmt
        elif cid in {
            "gdal:rasterize",
            "gdal:grid",
            "gdal:sindex",
            "gdal:fftfilter",
            "gdal:pca",
            "gdal:tasseledcap",
            "gdal:gcpregister",
            "gdal:rasterdiff",
            "gdal:pansharpen",
            "gdal:mask",
            "gdal:fvc",
            "gdal:calibrate",
            "gdal:dos1",
            "gdal:cloudreplace",
        }:
            # 这些能力都真实消费 format；此前 sindex/fftfilter 漏发 format，
            # 会导致 GUI 里选 COG/ENVI 仍被 Worker 静默写成 GTiff。
            params["format"] = out_fmt
            if cid in {"gdal:rasterize", "gdal:grid"}:
                raw_type = self.cmb_out_type.currentText()
                if "Default" not in raw_type:
                    params["output_type"] = raw_type.split()[0]
        elif is_vector_output(cid):
            # P5-02 矢量成果：GPKG / GeoJSON 由 OGR 驱动矩阵校验（GUI 预检 + Worker）
            params["format"] = out_fmt
        elif cid == "gdal:calc":
            # 表达式成果是真实栅格，格式选项必须下发给 worker（默认 GTiff）
            params["format"] = out_fmt
        elif cid == "gdal:statistics" and out_p.suffix.lower() != ".json":
            QMessageBox.warning(
                self,
                "输出格式",
                "统计报告是 JSON artifact，输出文件名请以 .json 结尾。",
            )
            return

        # P4-01：裁剪 / 重采样 / 输出网格
        grid_params, grid_error = self._collect_grid_params(
            is_warp="warp" in cid,
            capability_id=cid,
        )
        if grid_error is not None:
            QMessageBox.warning(self, "参数不全", grid_error)
            return
        params.update(grid_params)

        # P4-02：重叠优先级 / 分辨率策略 / NoData / separate / relativePaths / bandList
        p402_params, p402_error = self._collect_p402_params(cid)
        if p402_error is not None:
            QMessageBox.warning(self, "参数不全", p402_error)
            return
        params.update(p402_params)

        # 提交前用**与 Worker 相同**的契约归一化并校验：
        # 未知算法、别名冲突、小数截断、非法组合都在这里被拦下，
        # 不会提交成功后才在 Worker 侧失效或被静默忽略。
        try:
            params.update(normalize_raster_params(params, capability_id=cid))
        except ParameterError as exc:
            QMessageBox.warning(self, "参数不合法", str(exc))
            return

        format_error = self._preflight_output_format(cid, in_paths, out_fmt, params)
        if format_error:
            QMessageBox.warning(self, "输出格式不支持", format_error)
            return

        provider_id = "rasterio" if "rasterio" in cid or "rio" in cid else "gdal"
        runtime_id = "rasterio-venv" if provider_id == "rasterio" else "gdal-qgis"
        expected_version = "1.5.2" if provider_id == "rasterio" else "3.13.3"
        spec = TaskSpec(
            task_id=task_id,
            attempt=1,
            capability_id=cid,
            descriptor_revision=self._current_cap.descriptor_revision if self._current_cap else "1.0",
            engine=EngineConstraint(
                provider_id=provider_id,
                runtime_id=runtime_id,
                expected_version=expected_version,
            ),
            project_root=str(self._project_root),
            workspace=str(out_p.parent),
            inputs=(
                {
                    "inputs": [
                        DatasetRef(
                            dataset_id=f"in-{i}-{uuid.uuid4().hex[:6]}",
                            kind=DatasetKind.raster,
                            uri=p.resolve().as_uri(),
                        )
                        for i, p in enumerate(in_paths)
                    ]
                }
                if (multi or in_place)
                else {
                    "input": DatasetRef(
                        dataset_id=f"in-{uuid.uuid4().hex[:6]}",
                        kind=(
                            DatasetKind.vector
                            # 仅 ogr:vectortranslate 以矢量为输入；
                            # polygonize/contour 的输入是栅格
                            if cid == "ogr:vectortranslate"
                            else DatasetKind.raster
                        ),
                        uri=in_paths[0].as_uri(),
                    )
                }
            ),
            parameters=params,
            outputs=[
                OutputPlan(
                    key="output",
                    kind=(
                        DatasetKind.artifact
                        if cid == "gdal:statistics"
                        else DatasetKind.vector
                        if is_vector_output(cid)
                        else DatasetKind.raster
                    ),
                    uri=out_p.as_uri(),
                    overwrite=overwrite,
                )
            ],
        )

        self.sig_task_submitted.emit(spec)
