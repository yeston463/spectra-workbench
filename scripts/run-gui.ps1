[CmdletBinding()]
param(
    [switch]$Sample,
    [string]$Open,
    [string]$Screenshot,
    [switch]$Headless
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

$UiPython = Join-Path $ProjectRoot ".venvs\sw-ui\Scripts\python.exe"
if (-not (Test-Path $UiPython)) {
    $UiPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

if (-not (Test-Path $UiPython)) {
    Write-Error "未找到 UI Python 解释器: $UiPython"
    exit 1
}

$Arguments = @("-m", "spectra_workbench.ui.app", "--project-root", $ProjectRoot)

if ($Sample) {
    $Arguments += "--sample"
}
if ($Open) {
    $Arguments += @("--open", $Open)
}
if ($Screenshot) {
    $Arguments += @("--screenshot", $Screenshot)
}
if ($Headless) {
    $Arguments += "--headless"
}

Write-Host ">>> 正在启动 Spectra Workbench 桌面界面..." -ForegroundColor Cyan
Write-Host ">>> 解释器: $UiPython" -ForegroundColor Gray
Write-Host ">>> 工作目录: $ProjectRoot" -ForegroundColor Gray

$env:PYTHONPATH = (Join-Path $ProjectRoot "src") + ";" + $env:PYTHONPATH

& $UiPython $Arguments
$ExitCode = $LASTEXITCODE
exit $ExitCode
