#requires -Version 5.1
<#
  bootstrap_pip.ps1 — conda-forge 不可用时的环境落地路线（依据 docs/adr/0001-environment-strategy.md）。
  创建三个 pip venv（ui / rasterio / science），并把本机 QGIS/OSGeo4W 运行时登记为 GDAL 引擎运行时
  （只读使用，不修改该安装）。产出精确锁与 docs/progress/runs/env_report.json。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap_pip.ps1
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap_pip.ps1 -Only rasterio,science
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap_pip.ps1 -VerifyOnly
#>
[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [string]$GdalPython = 'C:\Program Files\QGIS 3.44.15\apps\Python312\python.exe',
    [string]$GdalBin = 'C:\Program Files\QGIS 3.44.15\bin',
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvDir     = Join-Path $ProjectRoot '.venvs'
$LockDir    = Join-Path $ProjectRoot 'environments\locks'
$RunDir     = Join-Path $ProjectRoot 'docs\progress\runs'
$Probe      = Join-Path $ProjectRoot 'environments\probe.py'
$LogFile    = Join-Path $RunDir 'bootstrap_pip.log'
$ReportFile = Join-Path $RunDir 'env_report.json'
$BasePython = 'C:\python\python.exe'

New-Item -ItemType Directory -Force -Path $LockDir, $RunDir, $EnvDir | Out-Null
$env:PYTHONIOENCODING = 'utf-8'

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

# 原生命令（python/pip）常往 stderr 写警告；在 Stop 下会被 PowerShell 抛成异常。
# 统一用这个包装器，返回输出与真实退出码。
function RunNative([scriptblock]$sb) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $o = & $sb 2>&1 | ForEach-Object { "$_" }
    $c = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return @{ out = $o; code = $c }
}

function Probe-Env($py, $key, $require) {
    # 原生命令的 stderr 在 ErrorActionPreference=Stop 下会被 PowerShell 抛成异常，
    # 这里临时放宽，改由 $LASTEXITCODE 与输出内容判定结果。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $py $Probe $key 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    $jsonLine = ($out | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
    $obj = $null
    if ($jsonLine) { try { $obj = $jsonLine | ConvertFrom-Json } catch { Log 'probe JSON 解析失败' } }
    $ok = ($code -eq 0) -and ($null -ne $obj) -and ($obj.packages.($require))
    return @{ ok = [bool]$ok; obj = $obj; raw = ($out -join "`n"); exit_code = $code }
}

$VenvSpecs = [ordered]@{
    ui       = @{ dir = 'sw-ui';       require = 'PySide6';  spec = 'ui-pip.txt' }
    rasterio = @{ dir = 'sw-rasterio'; require = 'rasterio'; spec = 'rasterio-pip.txt' }
    science  = @{ dir = 'sw-science';  require = 'numpy';    spec = 'science-pip.txt' }
}

$report = $null
if (Test-Path $ReportFile) {
    try { $report = Get-Content $ReportFile -Raw -Encoding utf8 | ConvertFrom-Json } catch { $report = $null }
}
if (-not $report) {
    $report = [pscustomobject]@{
        generated_utc = ''; strategy = 'pip venv + 本机 QGIS/OSGeo4W GDAL 运行时（开发期）'
        micromamba = $null; root_prefix = $EnvDir; verify_only = $false; envs = [ordered]@{}
    }
}
$report.generated_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
# 从磁盘读回的 envs 是 PSCustomObject，不能按名字索引赋值；统一归一成有序字典。
$envsDict = [ordered]@{}
if ($report.envs -is [System.Collections.IDictionary]) {
    foreach ($k in $report.envs.Keys) { $envsDict[$k] = $report.envs[$k] }
} elseif ($report.envs) {
    foreach ($p in $report.envs.PSObject.Properties) { $envsDict[$p.Name] = $p.Value }
}
$report | Add-Member -NotePropertyName envs -NotePropertyValue $envsDict -Force

$targets = if ($Only.Count -gt 0) { $Only } else { @('gdal') + @($VenvSpecs.Keys) }

foreach ($key in $targets) {
    Log ('=== {0}' -f $key)

    if ($key -eq 'gdal') {
        if (-not (Test-Path $GdalPython)) { Log 'FAIL 未找到 QGIS GDAL Python'; $report.envs['gdal'] = [ordered]@{ status = 'missing_runtime'; python = $GdalPython }; continue }
        $env:PATH = "$GdalBin;$env:PATH"
        $realGdalData = Join-Path (Split-Path $GdalBin) 'apps\gdal\share\gdal'
        if (-not (Test-Path $realGdalData)) { $realGdalData = Join-Path (Split-Path $GdalBin) 'share\gdal' }
        $env:GDAL_DATA = $realGdalData
        $env:PROJ_DATA = Join-Path (Split-Path $GdalBin) 'share\proj'
        $r = Probe-Env $GdalPython 'gdal' 'osgeo'
        Log ('import osgeo: {0}' -f $(if ($r.ok) { 'PASS' } else { 'FAIL' }))
        $cli = @{}
        foreach ($exe in @('gdal', 'gdalinfo', 'ogrinfo', 'gdal_translate', 'gdalwarp')) {
            $p = Join-Path $GdalBin ($exe + '.exe')
            if (Test-Path $p) { $vr = RunNative { & $p '--version' }; $v = ($vr.out -join ' '); $cli[$exe] = @{ path = $p; version_output = $v; exit_code = $vr.code } }
        }
        $report.envs['gdal'] = [ordered]@{
            status = $(if ($r.ok) { 'ok' } else { 'import_failed' })
            kind = 'external_runtime_read_only'
            env_name = 'QGIS 3.44.15 / OSGeo4W'
            python = $GdalPython
            bin = $GdalBin
            gdal_data = $env:GDAL_DATA
            proj_data = $env:PROJ_DATA
            cli = $cli
            probe = $r.obj
            raw_probe = $r.raw
            caveat = '开发期运行时；P0-05/P10 打包必须自带可重定位 GDAL 运行时，不得依赖本机 QGIS 安装'
            disk_bytes = 0
        }
        continue
    }

    if (-not $VenvSpecs.Contains($key)) { Log "跳过未知环境名：$key"; continue }
    $cfg    = $VenvSpecs[$key]
    $prefix = Join-Path $EnvDir $cfg.dir
    $py     = Join-Path $prefix 'Scripts\python.exe'

    if (-not $VerifyOnly -and -not (Test-Path $py)) {
        Log "创建 venv：$prefix"
        $rv = RunNative { & $BasePython '-m' 'venv' $prefix }
        $rv.out | Add-Content -Path $LogFile -Encoding utf8
        if ($rv.code -ne 0) { Log "FAIL venv exit=$($rv.code)"; $report.envs[$key] = [ordered]@{ status = 'venv_failed'; prefix = $prefix; exit_code = $rv.code }; continue }
        $null = RunNative { & $py '-m' 'pip' 'install' '--disable-pip-version-check' '--no-input' '--upgrade' 'pip' }
    }
    if (-not (Test-Path $py)) { Log "FAIL 解释器不存在 $py"; $report.envs[$key] = [ordered]@{ status = 'missing_interpreter'; prefix = $prefix }; continue }

    if (-not $VerifyOnly) {
        $req = Join-Path (Join-Path $ProjectRoot 'environments') $cfg.spec
        Log ('pip install -r {0}' -f $req)
        $r = RunNative { & $py '-m' 'pip' 'install' '--disable-pip-version-check' '--no-input' '-r' $req }
        $r.out | Add-Content -Path $LogFile -Encoding utf8
        if ($r.code -ne 0) { Log "FAIL pip install exit=$($r.code)"; $report.envs[$key] = [ordered]@{ status = 'install_failed'; prefix = $prefix; exit_code = $r.code }; continue }
    }

    $lock = Join-Path $LockDir ($cfg.dir + '.lock.txt')
    $fr = RunNative { & $py '-m' 'pip' 'freeze' }
    [System.IO.File]::WriteAllText($lock, ($fr.out -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))

    $r = Probe-Env $py $key $cfg.require
    Log ('import {0}: {1}' -f $cfg.require, $(if ($r.ok) { 'PASS' } else { 'FAIL' }))

    $isolation = @{}
    if ($key -eq 'ui' -and $r.obj) {
        foreach ($p in $r.obj.isolation.PSObject.Properties) {
            $isolation[$p.Name] = $(if ($p.Value) { 'LEAKED(违反隔离)' } else { 'absent(符合隔离)' })
            Log ('isolation {0}: {1}' -f $p.Name, $isolation[$p.Name])
        }
    }
    $cli = @{}
    $rioExe = Join-Path $prefix 'Scripts\rio.exe'
    if (Test-Path $rioExe) { $vr = RunNative { & $rioExe '--version' }; $v = ($vr.out -join ' '); $cli['rio'] = @{ path = $rioExe; version_output = $v; exit_code = $vr.code }; Log ('cli rio: {0}' -f $v) }

    $report.envs[$key] = [ordered]@{
        status = $(if ($r.ok) { 'ok' } else { 'import_failed' })
        kind = 'pip_venv'
        env_name = $cfg.dir
        prefix = $prefix
        python = $py
        spec = ('environments/' + $cfg.spec)
        lock = $lock
        require = $cfg.require
        import_ok = $r.ok
        isolation = $isolation
        cli = $cli
        probe = $r.obj
        raw_probe = $r.raw
        disk_bytes = (DirBytes $prefix)
        disk_gb = [math]::Round((DirBytes $prefix) / 1GB, 2)
    }
}

$json = $report | ConvertTo-Json -Depth 14
[System.IO.File]::WriteAllText($ReportFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Log "报告写入 $ReportFile"
$bad = @()
foreach ($k in @($report.envs.Keys)) {
    if ($report.envs[$k].status -ne 'ok') { $bad += $k }
}
if ($bad.Count -gt 0) { Log ('RESULT PARTIAL/FAIL 非 ok 环境=' + ($bad -join ',')); exit 3 }
Log 'RESULT ALL OK'
exit 0
