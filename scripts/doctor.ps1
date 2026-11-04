#requires -Version 5.1
<#
  doctor.ps1 — Spectra Workbench 引擎与环境自检流水线（P0-03）。
  对 UI、GDAL、Rasterio、Science 环境分别做完整自检：
  - GTiff 往返
  - 坐标转换
  - CLI 工具可用性
  - 资源目录（GDAL_DATA / PROJ_DATA）
  - 驱动分别报告；无 HDF4 仍可打开 GUI
  - UI 原生地理依赖隔离验证

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1 -Json
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1 -OutFile custom_report.json
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Python = Join-Path $ProjectRoot '.venvs\sw-ui\Scripts\python.exe'
}
if (-not (Test-Path $Python)) {
    Write-Error '未找到 Python 运行环境（.venv 或 .venvs/sw-ui）。请先运行 scripts\bootstrap_pip.ps1'
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'

$argsList = @('-m', 'spectra_workbench.app.doctor')
if ($Json) {
    $argsList += '--json'
}
if ($OutFile) {
    $argsList += @('--out', $OutFile)
}

& $Python $argsList
exit $LASTEXITCODE
