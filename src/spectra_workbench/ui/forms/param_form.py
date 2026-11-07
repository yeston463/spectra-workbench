"""从注册表动态构建算法与工具参数面板（P2-04）。

特性：
1. 完整接通 GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换。
2. 针对未接入平台的能力，保留完整说明、限制项与状态徽标，严格禁用提交按钮（禁止提交）。
3. 严格遵守 GUI 进程隔离要求，零原生地理库导入。
"""

from __future__ import annotations

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
    supports_output_format,
)
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
)
from spectra_workbench.processing.output_formats import validate_output_format
from spectra_workbench.processing.raster_params import (
    RESAMPLE_ALGORITHM_CHOICES,
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)

if TYPE_CHECKING:
    from spectra_workbench.domain.models import CapabilityDescriptor




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
            out_name = f"{p.stem}_processed.tif"
            self.txt_output.setText(str(p.parent / out_name))

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

        show_out_type = is_translate or "outputType" in sup
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
        self._set_row_visible(self.lbl_levels, self.txt_levels, "levels" in sup)
        self._set_row_visible(self.lbl_ovr_compress, self.cmb_ovr_compress, "overviewCompress" in sup)
        # P4-04
        self._set_row_visible(self.lbl_calc, self.txt_calc, "calc" in sup)
        self._set_row_visible(self.lbl_stats_mode, self.cmb_stats_mode, "statisticsMode" in sup)
        self._set_row_visible(self.lbl_hist_bins, self.spin_hist_bins, "histogramBins" in sup)
        self._set_row_visible(self.lbl_max_samples, self.spin_max_samples, "maxSamples" in sup)
        self._set_row_visible(self.lbl_full_limit, self.spin_full_limit, "fullLimit" in sup)

        # P4-01：裁剪 / 重采样 / 输出网格
        self._set_row_visible(self.lbl_clip, self.txt_clip, is_translate or is_warp)
        self._set_row_visible(self.lbl_xres, self.spin_xres, is_translate or is_warp)
        self._set_row_visible(self.lbl_yres, self.spin_yres, is_translate or is_warp)
        self._set_row_visible(self.lbl_outsize, self.txt_outsize, is_translate or is_warp)
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

    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择输入遥感影像",
            str(self._project_root),
            "栅格影像 (*.tif *.tiff *.dat *.img *.hdr *.png *.jpg);;所有文件 (*.*)",
        )
        if path:
            self.set_input_path(path)

    def _browse_output(self) -> None:
        init_dir = str(Path(self.txt_input.text()).parent) if self.txt_input.text() else str(self._project_root)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出影像文件",
            init_dir,
            "GeoTIFF (*.tif);;ENVI (*.dat);;所有文件 (*.*)",
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
        if capability_id == "gdal:dem":
            dem_violations = validate_output_format(
                out_fmt,
                dtype="Float32",
                bands=1,
                has_nodata=False,
                has_alpha=False,
            )
            return "；".join(dem_violations) if dem_violations else None
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
        fmt_map = {"GTiff": "GTiff", "ENVI": "ENVI", "COG": "COG", "PNG": "PNG", "JPEG": "JPEG"}
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
                        kind=DatasetKind.raster,
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
                        else DatasetKind.raster
                    ),
                    uri=out_p.as_uri(),
                    overwrite=overwrite,
                )
            ],
        )

        self.sig_task_submitted.emit(spec)
