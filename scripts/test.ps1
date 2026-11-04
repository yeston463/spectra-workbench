#requires -Version 5.1
<#
  test.ps1 — Spectra Workbench 自动化流水线（P0-04）。
  按序执行代码静态检查、类型检查与测试分组：
  1. Ruff Linting (ruff check src tests)
  2. Mypy Type Checking (mypy)
  3. Pytest (根据 -Marker / -Unit / -Integration / -Gui 分组运行)
     pytest 由 scripts/run_with_timeout.py 托管：超过 -TimeoutSeconds（默认 1200 秒）
     即结束整棵子进程树、保留日志与 faulthandler 堆栈，并以退出码 124 失败。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Quick
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Marker integration
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Gui
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -All
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -All -TimeoutSeconds 1800
#>
[CmdletBinding()]
param(
    [string]$Marker = 'unit',
    [switch]$Unit,
    [switch]$Integration,
    [switch]$Gui,
    [switch]$All,
    [switch]$Quick,
    [switch]$NoLint,
    [switch]$NoTypeCheck,
    [int]$TimeoutSeconds = 1200
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Python = Join-Path $ProjectRoot '.venvs\sw-ui\Scripts\python.exe'
}
if (-not (Test-Path $Python)) {
    Write-Error '未找到 Python 运行环境（.venv）。请先运行 bootstrap'
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'

# 判定测试 Marker
$targetMarker = $Marker
if ($Unit) { $targetMarker = 'unit' }
elseif ($Integration) { $targetMarker = 'integration' }
elseif ($Gui) { $targetMarker = 'gui' }
elseif ($All) { $targetMarker = '' }

function Run-Step([string]$title, [scriptblock]$action) {
    Write-Host "`n>>> [STEP] $title" -ForegroundColor Cyan
    & $action
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host ">>> [FAIL] $title (退出码: $code)" -ForegroundColor Red
        exit $code
    }
    Write-Host ">>> [PASS] $title" -ForegroundColor Green
}

# Step 1: Lint
if (-not $Quick -and -not $NoLint) {
    Run-Step 'Ruff 静态检查 (ruff check src tests)' {
        & $Python -m ruff check src tests
    }
}

# Step 2: Mypy
if (-not $Quick -and -not $NoTypeCheck) {
    Run-Step 'Mypy 核心类型检查 (mypy)' {
        & $Python -m mypy
    }
}

# Step 3: Pytest（进程级硬超时：挂起时保留日志与堆栈、清理本次子进程并返回 124）
$Guard = Join-Path $PSScriptRoot 'run_with_timeout.py'
$logName = if ($targetMarker) { "test_$targetMarker" } else { 'test_all' }
$RunLog = Join-Path $ProjectRoot ("work\test_runs\$logName.log")
$pytestArgs = @('-m', 'pytest', '-q', '-o', 'faulthandler_timeout=60')
if ($targetMarker -ne '') {
    $pytestArgs += @('-m', $targetMarker)
}

Run-Step ("Pytest 自动化测试 (marker: {0}, 硬超时 {1}s, 日志: {2})" -f `
    $(if ($targetMarker) { $targetMarker } else { 'ALL' }), $TimeoutSeconds, $RunLog) {
    & $Python $Guard --timeout $TimeoutSeconds --log $RunLog -- $Python @pytestArgs
}

Write-Host "`n=== ALL CHECKS PASSED ===" -ForegroundColor Green
exit 0
