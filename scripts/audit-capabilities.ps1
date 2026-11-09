#requires -Version 5.1
<#
  audit-capabilities.ps1 — Spectra Workbench 全能力基准与状态审计（P2-01 ~ P2-03）。
  对齐目标版本上游基准（GDAL 3.13.3 与 Rasterio 1.5.2），
  严格分离并输出四维独立状态：已发现 (discovered)、可调用 (callable)、已验证 (verified)、缺失 (missing)。
  基准分母非零，显式保留缺失能力条目，严禁静默过滤。
#>
[CmdletBinding()]
param(
    [string]$Provider = '',
    [string]$OutJson = 'docs/progress/runs/capability_audit.json',
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Python = Join-Path $ProjectRoot '.venvs\sw-ui\Scripts\python.exe'
}
if (-not (Test-Path $Python)) {
    Write-Error '未找到 Python 运行环境（sw-ui）。请先运行 bootstrap。'
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'

$auditPy = @"
import sys, json
from pathlib import Path
sys.path.insert(0, 'src')

from spectra_workbench.registry import get_default_registry

provider = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] not in ('', 'all') else None
out_file = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

reg = get_default_registry()
report = reg.full_audit_report(provider=provider)

if out_file:
    p = Path(out_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

print(json.dumps(report, ensure_ascii=False))
"@

$targetProvider = if ($Provider) { $Provider } else { "all" }
$targetOut = if ($OutJson) { $OutJson } else { "docs/progress/runs/capability_audit.json" }

& $Python -c $auditPy $targetProvider $targetOut
if ($LASTEXITCODE -ne 0) {
    Write-Error "能力审计脚本执行失败，退出码: $LASTEXITCODE"
    exit $LASTEXITCODE
}

$resolvedOut = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($targetOut)
$jsonText = [System.IO.File]::ReadAllText($resolvedOut, [System.Text.Encoding]::UTF8)
$data = $jsonText | ConvertFrom-Json

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "    Spectra Workbench · 全能力基准与覆盖审计 (P2)" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ("目标范围: {0}" -f $(if ($Provider) { $Provider } else { 'GDAL 3.13.3 + Rasterio 1.5.2 全量基准' }))
Write-Host ("公开基准总数 (分母): {0}" -f $data.total_baseline_count) -ForegroundColor Yellow
Write-Host ("  1. 已发现 (discovered): {0} ({1:P1})" -f $data.discovered.count, $data.discovered.ratio) -ForegroundColor Green
Write-Host ("  2. 可调用 (callable):   {0} ({1:P1})" -f $data.callable.count, $data.callable.ratio) -ForegroundColor Green
Write-Host ("  3. 已验证 (verified):   {0} ({1:P1})" -f $data.verified.count, $data.verified.ratio) -ForegroundColor Cyan
Write-Host ("  4. 缺失/受阻 (missing):  {0} ({1:P1})" -f $data.missing.count, $data.missing.ratio) -ForegroundColor Magenta

if ($data.baseline_reconciliation) {
    $rec = $data.baseline_reconciliation
    Write-Host ""
    Write-Host ">>> 基准逐项对账（P5-01）：" -ForegroundColor Yellow
    Write-Host ("  基准条目: {0} | 注册表条目: {1}" -f $rec.baseline_count, $rec.registry_count)
    Write-Host ("  基准有、注册表无: {0}" -f @($rec.missing_from_registry).Count)
    Write-Host ("  注册表平台扩展: {0}" -f @($rec.platform_only).Count)
    Write-Host ("  缺口条目: {0} | 未说明缺口: {1}" -f $rec.gap_count, @($rec.unexplained_gaps).Count)
    if (@($rec.missing_from_registry).Count -gt 0) {
        foreach ($missingId in $rec.missing_from_registry) {
            Write-Host ("    [缺失] {0}" -f $missingId) -ForegroundColor Red
        }
    }
    if (@($rec.platform_only).Count -gt 0) {
        Write-Host ("    平台扩展: {0}" -f ($rec.platform_only -join ', ')) -ForegroundColor Cyan
    }
    if ($rec.complete) {
        Write-Host "  对账结论: 基准全部登记，缺口均有具体原因" -ForegroundColor Green
    } else {
        Write-Host "  对账结论: 不完整，见 JSON 的 errors / unexplained_gaps" -ForegroundColor Red
    }
}

if ($data.missing.count -gt 0) {
    Write-Host ""
    Write-Host ">>> 显式保留的缺失/受阻清单（严禁静默过滤）：" -ForegroundColor Magenta
    foreach ($m in $data.missing.items) {
        $reasons = if ($m.limitations) { $m.limitations -join '; ' } else { '无说明' }
        Write-Host ("  - [{0}] {1} ({2}/{3}): {4}" -f $m.provider, $m.capability_id, $m.presence, $m.availability, $reasons)
    }
}

if (-not $Provider -and $data.by_provider) {
    Write-Host ""
    Write-Host ">>> 分引擎汇总：" -ForegroundColor Cyan
    foreach ($prop in $data.by_provider.PSObject.Properties) {
        $sub = $prop.Value
        Write-Host ("  - {0,-10} 总基准: {1,3} | 已发现: {2,3} | 可调用: {3,3} | 已验证: {4,3} | 缺失: {5,2}" -f $prop.Name, $sub.total_baseline_count, $sub.discovered.count, $sub.callable.count, $sub.verified.count, $sub.missing.count)
    }
}

if ($data.parameter_audit) {
    $pa = $data.parameter_audit
    Write-Host ""
    Write-Host ">>> 参数契约全量审计（P9-01）：" -ForegroundColor Yellow
    Write-Host ("  审计能力数: {0} | 审计参数总数: {1} | 已接通表单能力: {2}" -f $pa.total_capabilities_audited, $pa.total_parameters_audited, $pa.connected_capabilities_audited)
    Write-Host ("  参数漂移数: {0} | Schema错误: {1} | 别名冲突: {2} | 合规结论: {3}" -f @($pa.connected_parameter_drift).Count, @($pa.schema_errors).Count, @($pa.param_alias_collisions).Count, $pa.compliant)
}

Write-Host ""
Write-Host ("审计报告已落盘: {0}" -f $OutJson) -ForegroundColor Green
exit 0
