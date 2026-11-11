"""Integration test for P10-04 final release audit:

1. Capability checklist & all capability states / unverified items accuracy;
2. Numerical computation evidence & feature screenshot artifacts;
3. Distribution package reproducibility & manifest audit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectra_workbench.registry.catalog import get_default_registry
from spectra_workbench.registry.parameter_audit import build_full_audit_report

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "docs" / "progress" / "runs"
DIST_DIR = REPO_ROOT / "dist" / "SpectraWorkbench"


@pytest.mark.integration
def test_p10_04_checklist_and_capability_audit_compliance():
    """验证能力检查表审计：全量基准无未解释缺口，所有未验项/阻断项均有明确局限性与替代说明，参数 0 漂移。"""
    registry = get_default_registry()
    report = build_full_audit_report(registry)

    # 验证基准总数与发现率
    assert report["total_baseline_count"] == 468, f"基准总数异常: {report['total_baseline_count']}"
    assert report["discovered"]["count"] >= 455
    assert report["callable"]["count"] >= 250
    assert report["platform_executable"]["count"] >= 29

    # 核心红线：未解释缺口必须严格为 0
    assert len(report.get("unexplained_gaps", [])) == 0, (
        f"存在未解释缺口: {report.get('unexplained_gaps')}"
    )

    # 验证所有 missing / blocked 均包含充分的 limitations 局限性声明，杜绝用简化公式冒充
    missing_items = report["missing"]["items"]
    assert len(missing_items) == 13, f"缺失项计数不符: {len(missing_items)}"
    for item in missing_items:
        assert item.get("limitations") and len(item["limitations"]) > 0, (
            f"能力 {item['capability_id']} 缺少 limitations 说明"
        )
        # 验证说明包含具体替代或未实现原因
        text = " ".join(item["limitations"])
        assert any(
            k in text
            for k in ["未实现", "替代", "不承诺", "sw-rasterio", "QGIS", "blocked", "外部"]
        ), f"能力 {item['capability_id']} 的 limitations 描述不充分: {text}"


@pytest.mark.integration
def test_p10_04_computation_evidence_numerical_integrity():
    """验证数值计算证据完整性：坐标转换、Hillshade、NDVI、PCA、FFT 与 FVC 六大算法数值证明 100% 通过。"""
    comp_file = RUNS_DIR / "computation_evidence.json"
    assert comp_file.exists(), "缺少数值计算证据文件 docs/progress/runs/computation_evidence.json"

    evidence = json.loads(comp_file.read_text(encoding="utf-8"))
    algos = evidence.get("algorithms", {})

    assert len(algos) == 6, f"计算证据算法数不足: {len(algos)}"

    # 1. 坐标转换
    crs = algos["crs_transformation"]
    assert crs["verified"] is True
    assert crs["error_meters"] < 0.001

    # 2. 地形 Hillshade
    dem = algos["dem_hillshade"]
    assert dem["verified"] is True
    assert 0 <= dem["theoretical_dn_byte"] <= 255

    # 3. 植被指数 NDVI
    ndvi = algos["spectral_ndvi"]
    assert ndvi["verified"] is True
    assert ndvi["absolute_error"] < 1e-9

    # 4. PCA 无损重建
    pca = algos["pca_reconstruction"]
    assert pca["verified"] is True
    assert pca["max_reconstruction_error"] <= 1e-10

    # 5. 2D FFT 频域滤波
    fft = algos["fft_filtering"]
    assert fft["verified"] is True
    assert fft["suppression_residual"] <= 1e-10

    # 6. FVC 像元二分
    fvc = algos["fvc_pixel_bisection"]
    assert fvc["verified"] is True
    assert abs(fvc["computed_fvc"] - 0.5) < 1e-9


@pytest.mark.integration
def test_p10_04_feature_screenshots_and_artifacts():
    """验证主要功能离屏渲染截图完整性（桌面全景、双视口对比、交互式 ROI）。"""
    expected_shots = [
        "desktop_ui_preview.png",
        "desktop_ui_dual_view.png",
        "desktop_ui_roi_interactive.png",
    ]

    for shot_name in expected_shots:
        shot_path = RUNS_DIR / shot_name
        assert shot_path.exists(), f"缺少功能截图: {shot_path}"
        # 必须是有效的 PNG 二进制且大于 50KB（证明真实渲染）
        data = shot_path.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), f"文件不是有效 PNG 图像: {shot_name}"
        assert len(data) > 50000, f"截图文件过小，可能未正确渲染: {len(data)} bytes"


@pytest.mark.integration
def test_p10_04_package_reproducibility_and_manifest():
    """验证发布包可复建性、原生 PE 启动器完整性与最终审计报告状态。"""
    # 1. 检查 release_audit.json 状态
    audit_file = RUNS_DIR / "release_audit.json"
    assert audit_file.exists(), "缺少最终发布审计报告 release_audit.json"
    audit = json.loads(audit_file.read_text(encoding="utf-8"))

    assert audit["status"] == "APPROVED"
    assert audit["release_milestone"] == "P10"
    assert audit["capability_audit_summary"]["overall_compliant"] is True
    assert audit["computation_evidence_summary"]["all_passed"] is True

    # 2. 检查发布包结构
    assert DIST_DIR.is_dir(), f"发布包目录不存在: {DIST_DIR}"
    assert (DIST_DIR / "manifest.json").exists()

    manifest = json.loads((DIST_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.0.0"
    assert manifest["stats"]["gdal_bin_files"] >= 160
    assert manifest["stats"]["proj_share_files"] >= 10

    # 3. 检查原生启动器二进制
    for exe in ["SpectraWorkbench.exe", "SpectraWorkbench-console.exe", "uninstall.exe"]:
        p = DIST_DIR / exe
        assert p.exists(), f"发布包缺少关键可执行文件: {exe}"
        # 校验 PE 头部 MZ 标识
        header = p.read_bytes()[:2]
        assert header == b"MZ", f"{exe} 不是有效的 Windows PE 可执行文件"
