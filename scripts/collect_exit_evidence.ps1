#requires -Version 5.1
<#
  collect_exit_evidence.ps1 — 收集 Python/pytest 退出异常的本机证据。

  默认只读：
  - Application 事件日志（Id 1000/1001/1002）中的 python.exe 崩溃；
  - C:\ProgramData\Microsoft\Windows\WER\ReportArchive/ReportQueue 中相关报告；
  - 当前 WER LocalDumps 配置状态。

  不自动修改系统配置。管理员显式传入 -ApplyLocalDumps 时，才写入
  HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\python.exe，
  DumpType=2（完整 dump），目录固定在本项目 work/exit_dumps。

  注意：dump 可能包含业务数据和路径，只保存在本机 work/，不要提交到公开仓库。
#>
[CmdletBinding()]
param(
    [int]$SinceMinutes = 180,
    [string]$OutputDir = '',
    [switch]$ApplyLocalDumps
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $ProjectRoot 'work\exit_evidence'
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$RunDir = Join-Path $OutputDir "run-$Stamp"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$Since = (Get-Date).AddMinutes(-1 * [Math]::Abs($SinceMinutes))

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$localDumpsKey = 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps'
$pythonDumpsKey = Join-Path $localDumpsKey 'python.exe'
$localDumpsConfigured = Test-Path $pythonDumpsKey
$dumpFolder = Join-Path $ProjectRoot 'work\exit_dumps'
$applyResult = 'not-requested'
if ($ApplyLocalDumps) {
    if (-not $isAdmin) {
        $applyResult = 'blocked:not-admin'
    } else {
        New-Item -Path $pythonDumpsKey -Force | Out-Null
        New-Item -ItemType Directory -Force -Path $dumpFolder | Out-Null
        New-ItemProperty -Path $pythonDumpsKey -Name DumpFolder -PropertyType ExpandString `
            -Value $dumpFolder -Force | Out-Null
        New-ItemProperty -Path $pythonDumpsKey -Name DumpType -PropertyType DWord `
            -Value 2 -Force | Out-Null
        $localDumpsConfigured = $true
        $applyResult = 'applied'
    }
}

# 1) Application 事件日志
$events = @()
try {
    $rawEvents = Get-WinEvent -FilterHashtable @{
        LogName = 'Application'
        Id = 1000, 1001, 1002
        StartTime = $Since
    } -ErrorAction SilentlyContinue
    foreach ($ev in @($rawEvents)) {
        if ($null -eq $ev) { continue }
        $xmlPath = Join-Path $RunDir ("event-{0}-{1}.xml" -f $ev.Id, $ev.RecordId)
        try { $ev.ToXml() | Set-Content -Encoding UTF8 $xmlPath } catch { }
        $fields = @($ev.Properties | ForEach-Object { [string]$_.Value })
        $app = if ($fields.Count -gt 0) { $fields[0] } else { '' }
        if ($app -notmatch 'python|pytest|spectra') { continue }
        $events += [ordered]@{
            id = $ev.Id
            time = $ev.TimeCreated.ToString('o')
            record_id = $ev.RecordId
            faulting_app = $app
            faulting_module = if ($fields.Count -gt 3) { $fields[3] } else { '' }
            exception_code = if ($fields.Count -gt 6) { $fields[6] } else { '' }
            fault_offset = if ($fields.Count -gt 7) { $fields[7] } else { '' }
            process_id = if ($fields.Count -gt 8) { $fields[8] } else { '' }
            app_path = if ($fields.Count -gt 10) { $fields[10] } else { '' }
            module_path = if ($fields.Count -gt 11) { $fields[11] } else { '' }
            report_id = if ($fields.Count -gt 12) { $fields[12] } else { '' }
            xml = $xmlPath
        }
    }
} catch { }

# 2) WER 报告目录（只复制相关目录）
$werReports = @()
$werRoots = @(
    'C:\ProgramData\Microsoft\Windows\WER\ReportArchive',
    'C:\ProgramData\Microsoft\Windows\WER\ReportQueue'
)
$werDest = Join-Path $RunDir 'wer'
New-Item -ItemType Directory -Force -Path $werDest | Out-Null
foreach ($root in $werRoots) {
    if (-not (Test-Path $root)) { continue }
    $dirs = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LastWriteTime -ge $Since -and
            ($_.Name -match 'python|pytest|spectra|ucrtbase|qt6')
        }
    foreach ($d in @($dirs)) {
        $dest = Join-Path $werDest $d.Name
        try {
            Copy-Item -Path $d.FullName -Destination $dest -Recurse -Force
            $dumps = @(Get-ChildItem -Path $dest -Filter *.dmp -Recurse -ErrorAction SilentlyContinue)
            $werReports += [ordered]@{
                name = $d.Name
                source = $d.FullName
                copied_to = $dest
                report_wer = (Test-Path (Join-Path $dest 'Report.wer'))
                dumps = @($dumps | ForEach-Object { $_.FullName })
            }
        } catch {
            $werReports += [ordered]@{
                name = $d.Name
                source = $d.FullName
                copied_to = $null
                error = $_.Exception.Message
            }
        }
    }
}

$dumpFiles = @(Get-ChildItem -Path $werDest -Filter *.dmp -Recurse -ErrorAction SilentlyContinue)
$summary = [ordered]@{
    generated_at = (Get-Date).ToString('o')
    since_minutes = [Math]::Abs($SinceMinutes)
    output_dir = $RunDir
    is_admin = $isAdmin
    local_dumps_configured = $localDumpsConfigured
    local_dumps_key = $pythonDumpsKey
    dump_folder = $dumpFolder
    apply_local_dumps = $applyResult
    application_events = $events
    wer_reports = $werReports
    native_dump_available = ($dumpFiles.Count -gt 0)
    native_dump_files = @($dumpFiles | ForEach-Object { $_.FullName })
    notes = @(
        'WER LocalDumps under HKLM normally needs administrator rights.',
        'Do not commit dumps: they may contain local paths or data.',
        'Faulting DLL name alone is not a root cause; use the dump/thread stack to classify the fast-fail.'
    )
}
$summaryPath = Join-Path $RunDir 'summary.json'
$json = $summary | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($summaryPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "EXIT_EVIDENCE=$summaryPath"
Write-Host ("application_events={0} wer_reports={1} native_dump_available={2}" -f `
    @($events).Count, @($werReports).Count, $summary.native_dump_available)
exit 0
