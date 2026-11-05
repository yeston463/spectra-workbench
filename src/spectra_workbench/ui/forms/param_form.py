"""从注册表动态构建算法与工具参数面板（P2-04）。

特性：
1. 完整接通 GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换。
2. 针对未接入平台的能力，保留完整说明、限制项与状态徽标，严格禁用提交按钮（禁止提交）。
3. 严格遵守 GUI 进程隔离要求，零原生地理库导入。
"""

from __future__ import annotations

import uuid
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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
)

if TYPE_CHECKING:
    from spectra_workbench.domain.models import CapabilityDescriptor


CONNECTED_CAPABILITIES = {
    "gdal:translate",
    "gdal_translate",
    "gdal:warp",
    "gdalwarp",
    "rasterio:convert",
    "rio:cli:convert",
}


class CapabilityFormWidget(QWidget):
    """能力参数动态表单控件。"""

    sig_task_submitted = Signal(object)  # TaskSpec

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_cap: CapabilityDescriptor | None = None
        self._project_root = Path.cwd()

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
        self.params_form.addRow("输入影像 (Input):", in_row)

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
        self.cmb_resampling.addItems(["bilinear (双线性插值)", "near (最邻近)", "cubic (三次卷积)", "lanczos", "average"])
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

        # 显示或隐藏相应字段
        self.lbl_format.setVisible(is_translate or is_warp or is_rasterio)
        self.cmb_format.setVisible(is_translate or is_warp or is_rasterio)

        self.lbl_out_type.setVisible(is_translate)
        self.cmb_out_type.setVisible(is_translate)

        self.lbl_co.setVisible(is_translate)
        self.txt_co.setVisible(is_translate)

        self.lbl_dst_crs.setVisible(is_warp)
        self.cmb_dst_crs.setVisible(is_warp)

        self.lbl_resampling.setVisible(is_warp)
        self.cmb_resampling.setVisible(is_warp)

        # P4-01：裁剪 / 重采样 / 输出网格
        self._set_row_visible(self.lbl_clip, self.txt_clip, is_translate or is_warp)
        self._set_row_visible(self.lbl_xres, self.spin_xres, is_translate or is_warp)
        self._set_row_visible(self.lbl_yres, self.spin_yres, is_translate or is_warp)
        self._set_row_visible(self.lbl_outsize, self.txt_outsize, is_translate or is_warp)
        self._set_row_visible(self.lbl_tap, self.chk_tap, is_warp)

    @staticmethod
    def _set_row_visible(label: QLabel, widget: QWidget, visible: bool) -> None:
        """同时显隐 QFormLayout 里的标签与控件。"""
        label.setVisible(visible)
        widget.setVisible(visible)

    def _collect_grid_params(self, is_warp: bool) -> tuple[dict[str, Any], str | None]:
        """采集 P4-01 的裁剪 / 重采样 / 输出网格参数。

        返回 (参数字典, 错误消息)。错误消息非 None 表示参数非法，调用方应提示并中止提交。
        空值一律不产生参数（绝不传 0 或空串给引擎）。
        """
        params: dict[str, Any] = {}

        clip_text = self.txt_clip.text().strip()
        if clip_text:
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

        x_res = float(self.spin_xres.value()) if self.spin_xres.value() > 0 else None
        y_res = float(self.spin_yres.value()) if self.spin_yres.value() > 0 else None
        if x_res is not None:
            params["xRes"] = x_res
        if y_res is not None:
            params["yRes"] = y_res

        outsize_text = self.txt_outsize.text().strip()
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

        if self.chk_tap.isChecked() and is_warp:
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

    def _on_submit_clicked(self) -> None:
        if not self._current_cap:
            return

        in_str = self.txt_input.text().strip()
        out_str = self.txt_output.text().strip()

        if not in_str:
            QMessageBox.warning(self, "参数不全", "请指定输入影像路径！")
            return
        if not out_str:
            QMessageBox.warning(self, "参数不全", "请指定输出成果路径！")
            return

        in_p = Path(in_str).resolve()
        out_p = Path(out_str).resolve()

        if not in_p.exists():
            QMessageBox.warning(self, "输入不存在", f"输入文件不存在:\n{in_p}")
            return
        if in_p == out_p:
            QMessageBox.critical(self, "同源冲突", "输入路径与输出路径指向同一文件，操作已被安全拒绝！")
            return

        # 组装 TaskSpec
        cid = self._current_cap.capability_id
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
            params["resampling"] = self.cmb_resampling.currentText().split()[0]
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

        # P4-01：裁剪 / 重采样 / 输出网格
        grid_params, grid_error = self._collect_grid_params(is_warp="warp" in cid)
        if grid_error is not None:
            QMessageBox.warning(self, "参数不全", grid_error)
            return
        params.update(grid_params)

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
            inputs={
                "input": DatasetRef(
                    dataset_id=f"in-{uuid.uuid4().hex[:6]}",
                    kind=DatasetKind.raster,
                    uri=in_p.as_uri(),
                )
            },
            parameters=params,
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_p.as_uri(),
                    overwrite=overwrite,
                )
            ],
        )

        self.sig_task_submitted.emit(spec)
