#requires -Version 5.1
<#
  test.ps1 — Spectra Workbench 自动化流水线（P0-04）。
  按序执行代码静态检查、类型检查与测试分组：
  1. Ruff Linting (ruff check src tests)
  2. Mypy Type Checking (mypy)
  3. Pytest (根据 -Marker / -Unit / -Integration / -Gui 分组运行)
     pytest 由 scripts/run_with_timeout.py 托管：超过 -TimeoutSeconds（默认 1200 秒）
     即结束整棵子进程树、保留日志与 faulthandler 堆栈，并以退出码 124 失败。
     默认 Clean：完整执行 finalization，由外层监督器写统一结果并验证
     clean_exit_verified；Bypass 仅用于临时功能验证，功能通过也返回 91，
     并标记 clean_exit_verified=false，不满足发布门槛。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Quick
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Marker integration
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Gui
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -All
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -All -TimeoutSeconds 1800
    powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -All -BypassFinalization
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
    [switch]$BypassFinalization,
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
$Runner = Join-Path $PSScriptRoot 'pytest_runner.py'
$logName = if ($targetMarker) { "test_$targetMarker" } else { 'test_all' }
$RunLog = Join-Path $ProjectRoot ("work\test_runs\$logName.log")
$ResultJson = Join-Path $ProjectRoot ("work\test_runs\$logName.result.json")
$ChildResultJson = Join-Path $ProjectRoot ("work\test_runs\$logName.child.result.json")
$CleanupReport = Join-Path $ProjectRoot ("work\test_runs\$logName.cleanup.json")
$ExitMode = if ($BypassFinalization) { 'bypass' } else { 'clean' }
if ($BypassFinalization) {
    Write-Host ">>> [WARN] BypassFinalization：跳过 finalization；clean_exit_verified=false，不满足发布门槛" -ForegroundColor Yellow
}
Remove-Item -Force -ErrorAction SilentlyContinue $ResultJson, $ChildResultJson, $CleanupReport
# 使用项目内独立 basetemp：系统临时目录（%TEMP%\pytest-of-*）里历史残留的
# pytest-current 断链会让 pytest_sessionfinish 的 cleanup_dead_symlinks 抛
# WinError 5，表现为「全部测试通过但 rc=1」。这与测试内容无关，属于环境问题；
# 每次用一个全新的项目内目录即可稳定规避（work/ 已被 .gitignore 忽略）。
$runStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$baseTemp = Join-Path $ProjectRoot ("work\test_runs\pytest_tmp\run-$runStamp")
# pytest 用 os.mkdir(basetemp)（parents=False），父目录必须先存在
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $baseTemp) | Out-Null
# child result 与 supervisor final 必须是不同文件；身份由 supervisor 通过环境变量注入。
$pytestArgs = @(
    $Runner, '--mode', $ExitMode, '--result-json', $ChildResultJson,
    '--', '-q', '-o', 'faulthandler_timeout=60', '--basetemp', $baseTemp
)
if ($targetMarker -ne '') {
    $pytestArgs += @('-m', $targetMarker)
}

Run-Step ("Pytest 自动化测试 (marker: {0}, exit_mode: {1}, 硬超时 {2}s, 日志: {3})" -f `
    $(if ($targetMarker) { $targetMarker } else { 'ALL' }), $ExitMode, $TimeoutSeconds, $RunLog) {
    & $Python $Guard --timeout $TimeoutSeconds --log $RunLog `
        --result-json $ResultJson --child-result-json $ChildResultJson `
        --cleanup-report-json $CleanupReport --exit-mode $ExitMode `
        -- $Python @pytestArgs
}

if (-not (Test-Path $ResultJson)) {
    Write-Host ">>> [FAIL] 缺少统一退出结果：$ResultJson" -ForegroundColor Red
    exit 90
}
$ExitResult = Get-Content -Raw -Encoding UTF8 $ResultJson | ConvertFrom-Json
Write-Host ("    exit_mode={0} actual_exit_mode={1} pytest_exit_code={2} child_exit_code={3} clean_exit_verified={4}" -f `
    $ExitResult.exit_mode, $ExitResult.actual_exit_mode, $ExitResult.pytest_exit_code, `
    $ExitResult.child_exit_code, $ExitResult.clean_exit_verified)
Write-Host ("    process_observation={0} residual={1} cleanup_complete={2} cleanup_errors={3}" -f `
    $ExitResult.process_observation, @($ExitResult.residual_processes).Count, `
    $ExitResult.cleanup_check_complete, @($ExitResult.cleanup_errors).Count)

if ($ExitMode -eq 'clean' -and -not $ExitResult.clean_exit_verified) {
    Write-Host ">>> [FAIL] Clean 退出未验证：finalization 未正常完成或存在残留/清理错误" -ForegroundColor Red
    Write-Host ("    结果：{0}" -f $ResultJson) -ForegroundColor Red
    exit 90
}
if ($ExitMode -eq 'bypass') {
    Write-Host "=== FUNCTIONAL CHECKS PASSED (BYPASS; NOT RELEASE ELIGIBLE) ===" -ForegroundColor Yellow
    Write-Host "    Bypass 结果仅证明用例执行，不证明正常退出；退出码 91" -ForegroundColor Yellow
    exit 91
}

Write-Host "`n=== ALL CHECKS PASSED (CLEAN EXIT VERIFIED) ===" -ForegroundColor Green
exit 0
