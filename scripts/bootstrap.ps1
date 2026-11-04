#requires -Version 5.1
<#
  bootstrap.ps1 — 创建项目内的四个独立环境（ui / gdal / rasterio / science），
  精确锁定版本，并逐环境在独立进程中验证引擎导入。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Only gdal,rasterio
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -VerifyOnly

  约束：包管理器与环境全部落在项目 .tooling\ 下；不修改系统 PATH，不动全局 Python。
#>
[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Tooling     = Join-Path $ProjectRoot '.tooling'
$Mamba       = Join-Path $Tooling 'bin\micromamba.exe'
$RootPrefix  = Join-Path $Tooling 'envs'
$LockDir     = Join-Path $ProjectRoot 'environments\locks'
$Probe       = Join-Path $ProjectRoot 'environments\probe.py'
$RunDir      = Join-Path $ProjectRoot 'docs\progress\runs'
$LogFile     = Join-Path $RunDir 'bootstrap.log'
$ReportFile  = Join-Path $RunDir 'env_report.json'

New-Item -ItemType Directory -Force -Path $LockDir, $RunDir | Out-Null
$env:MAMBA_ROOT_PREFIX = $RootPrefix
$env:PYTHONIOENCODING  = 'utf-8'

if (-not (Test-Path $Mamba)) {
    Write-Error "缺少 $Mamba ：请先获取 micromamba（conda-forge win-64）并解压到 .tooling\bin\micromamba.exe"
}

$Specs = [ordered]@{
    ui       = @{ spec = 'ui.yml';       env = 'sw-ui';       require = 'PySide6' }
    gdal     = @{ spec = 'gdal.yml';     env = 'sw-gdal';     require = 'osgeo' }
    rasterio = @{ spec = 'rasterio.yml'; env = 'sw-rasterio'; require = 'rasterio' }
    science  = @{ spec = 'science.yml';  env = 'sw-science';  require = 'numpy' }
}

function Log($msg) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function DirBytes($path) {
    if (-not (Test-Path $path)) { return 0 }
    $s = (Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
    if ($null -eq $s) { return 0 }
    return [int64]$s
}

$targets = if ($Only.Count -gt 0) { $Only } else { @($Specs.Keys) }
$report = [ordered]@{
    generated_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    micromamba    = ((& $Mamba --version) -join '')
    root_prefix   = $RootPrefix
    verify_only   = [bool]$VerifyOnly
    envs          = [ordered]@{}
}

foreach ($key in $targets) {
    if (-not $Specs.Contains($key)) { Log "跳过未知环境名：$key"; continue }
    $cfg    = $Specs[$key]
    $prefix = Join-Path $RootPrefix $cfg.env
    $py     = Join-Path $prefix 'python.exe'
    Log ('=== {0} ({1})' -f $key, $cfg.env)

    if (-not $VerifyOnly -and -not (Test-Path $py)) {
        $specPath = Join-Path $ProjectRoot ('environments\' + $cfg.spec)
        Log "创建环境：$prefix"
        & $Mamba 'create' '-y' '-q' '--prefix' $prefix '-f' $specPath
        if ($LASTEXITCODE -ne 0) {
            Log "FAIL 创建失败 exit=$LASTEXITCODE"
            $report.envs[$key] = [ordered]@{ status = 'create_failed'; prefix = $prefix; exit_code = $LASTEXITCODE }
            continue
        }
    }
    if (-not (Test-Path $py)) {
        Log "FAIL 解释器不存在：$py"
        $report.envs[$key] = [ordered]@{ status = 'missing_interpreter'; prefix = $prefix }
        continue
    }

    $lockExp = Join-Path $LockDir ($cfg.env + '.lock-explicit.txt')
    $lexp = & $Mamba 'list' '--prefix' $prefix '--export'
    [System.IO.File]::WriteAllText($lockExp, ($lexp -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    $lockYml = Join-Path $LockDir ($cfg.env + '.lock.yml')
    $lyml = & $Mamba 'env' 'export' '--no-history' '--prefix' $prefix
    [System.IO.File]::WriteAllText($lockYml, ($lyml -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))

    $probeOut = & $py $Probe $key 2>&1
    $probe = $null
    $lastJson = ($probeOut | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
    if ($lastJson) { try { $probe = $lastJson | ConvertFrom-Json } catch { Log "probe JSON 解析失败：$lastJson" } }
    $importOk = ($null -ne $probe) -and ($probe.packages.($cfg.require))
    Log ('probe import {0}: {1}' -f $cfg.require, $(if ($importOk) { 'PASS ' + $probe.packages.($cfg.require) } else { 'FAIL' }))

    $isolation = @{}
    if ($key -eq 'ui' -and $null -ne $probe) {
        foreach ($p in $probe.isolation.PSObject.Properties) {
            $isolation[$p.Name] = $(if ($p.Value) { 'LEAKED(违反隔离)' } else { 'absent(符合隔离)' })
            Log ('isolation {0}: {1}' -f $p.Name, $isolation[$p.Name])
        }
    }

    $cli = @{}
    foreach ($exe in @('gdal', 'gdalinfo', 'ogrinfo', 'rio')) {
        foreach ($cand in @((Join-Path $prefix ('Library\bin\' + $exe + '.exe')), (Join-Path $prefix ($exe + '.exe')))) {
            if (Test-Path $cand) {
                $v = (& $cand '--version' 2>&1) -join ' '
                $cli[$exe] = @{ path = $cand; version_output = $v; exit_code = $LASTEXITCODE }
                Log ('cli {0}: {1}' -f $exe, $v)
                break
            }
        }
    }

    $report.envs[$key] = [ordered]@{
        status        = $(if ($importOk) { 'ok' } else { 'import_failed' })
        env_name      = $cfg.env
        prefix        = $prefix
        python        = $py
        spec          = ('environments/' + $cfg.spec)
        lock_explicit = $lockExp
        lock_yml      = $lockYml
        require       = $cfg.require
        import_ok     = [bool]$importOk
        isolation     = $isolation
        cli           = $cli
        probe         = $probe
        raw_probe     = ($probeOut -join "`n")
        disk_bytes    = (DirBytes $prefix)
        disk_gb       = [math]::Round((DirBytes $prefix) / 1GB, 2)
    }
}

$json = $report | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText($ReportFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Log "报告写入 $ReportFile"
$bad = @($report.envs.Values | Where-Object { $_.status -ne 'ok' })
if ($bad.Count -gt 0) {
    Log ("RESULT PARTIAL/FAIL 非 ok 环境数={0}" -f $bad.Count)
    exit 3
}
Log 'RESULT ALL OK'
exit 0
