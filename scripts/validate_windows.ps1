<#
.SYNOPSIS
    Validate RaidWatch's Windows-only metric paths on the current host.

.DESCRIPTION
    Reports whether each Windows-only data source is producing real values
    vs. silently degrading to null:
        - CPU temp           : system.temp_cpu_celsius       (LHM via pythonnet)
        - WHEA events        : system.whea_count_2h           (win32evtlog)
        - PerfMon counters   : system.disk_queue_length, system.pages_per_sec,
                               system.disk_avg_sec_per_transfer   (win32pdh)

    Reads the auth token from the ACL'd config.yaml and calls the local API.
    Exit code 0 always (this is a report, not a pass/fail gate). Safe any time.

.PARAMETER BaseUrl
    Base URL of the RaidWatch service. Default: http://localhost:8080

.EXAMPLE
    .\scripts\validate_windows.ps1
#>
param([string]$BaseUrl = "http://localhost:8080")

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Config   = Join-Path $RepoRoot "data\config.yaml"

if (-not (Test-Path $Config)) {
    Write-Host "config not found at $Config -- is the service installed here?" -ForegroundColor Red
    return
}

# --- extract token (handles quoted or unquoted) ---
$tokLine = (Get-Content $Config | Select-String -Pattern '^\s*token:').Line
$token   = ([regex]::Match($tokLine, 'token:\s*"?([^"\s]+)"?')).Groups[1].Value
if (-not $token) {
    Write-Host "could not parse auth token from $Config" -ForegroundColor Red
    return
}
$headers = @{ Cookie = "raidwatch_token=$token" }

function Get-Json($path) {
    try { return Invoke-RestMethod "$BaseUrl$path" -Headers $headers -TimeoutSec 10 }
    catch {
        Write-Host "  (request $path failed: $($_.Exception.Message))" -ForegroundColor Red
        return $null
    }
}

function Report($label, $value, $nullMeans) {
    if ($null -eq $value) {
        Write-Host ("  [NULL] {0,-30} {1}" -f $label, $nullMeans) -ForegroundColor Yellow
    } else {
        Write-Host ("  [ OK ] {0,-30} = {1}" -f $label, $value) -ForegroundColor Green
    }
}

Write-Host "=== RaidWatch Windows validation ===" -ForegroundColor Cyan
Write-Host "endpoint: $BaseUrl`n"

# --- health (no auth needed) ---
$h = Get-Json "/health"
if ($h) {
    Write-Host "service status : $($h.status)" -ForegroundColor Cyan
    foreach ($m in 'system','temps','fika') {
        $ms = $h.modules.$m
        $err = if ($ms.last_error) { "  last_error: $($ms.last_error)" } else { "" }
        $clr = if ($ms.state -eq 'ok') { 'Gray' } else { 'Yellow' }
        Write-Host ("  module {0,-7} state={1}{2}" -f $m, $ms.state, $err) -ForegroundColor $clr
    }
}

# --- current snapshot (raw, not envelope-wrapped) ---
$s = Get-Json "/api/metrics/current"
if (-not $s -or $s.ok -eq $false) {
    Write-Host "`nno snapshot yet (collector may still be starting): $($s.error)" -ForegroundColor Yellow
    return
}
$sys = $s.system

Write-Host ""
Write-Host "--- pywin32 PerfMon counters (win32pdh) ---" -ForegroundColor Cyan
Report "disk_queue_length"           $sys.disk_queue_length           "win32pdh counter not returning"
Report "disk_avg_sec_per_transfer"   $sys.disk_avg_sec_per_transfer   "win32pdh counter not returning"
Report "pages_per_sec"               $sys.pages_per_sec               "win32pdh counter not returning"

Write-Host ""
Write-Host "--- WHEA hardware errors (win32evtlog) ---" -ForegroundColor Cyan
Report "whea_count_2h"               $sys.whea_count_2h               "win32evtlog not collecting"

Write-Host ""
Write-Host "--- CPU temperature (LHM via pythonnet) ---" -ForegroundColor Cyan
Report "temp_cpu_celsius"            $sys.temp_cpu_celsius            "LHM DLLs likely not vendored (AGENTS.md #2)"

Write-Host ""
Write-Host "--- Fika process discovery (expected empty on a dev box) ---" -ForegroundColor Cyan
$sptPid = $s.fika.spt_server.pid
if ($sptPid) {
    Write-Host ("  [ OK ] spt_server.pid            = {0}" -f $sptPid) -ForegroundColor Green
} else {
    Write-Host "  [ -- ] spt_server.pid            none (no SPT/Fika running here)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "--- raw system object (for full detail) ---" -ForegroundColor Cyan
$sys | ConvertTo-Json -Depth 5 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }

Write-Host "`n=== done ===" -ForegroundColor Cyan
