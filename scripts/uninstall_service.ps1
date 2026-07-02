<#
.SYNOPSIS
    Uninstall the RaidWatch Windows service and clean up.

.DESCRIPTION
    Removes the NSSM service, firewall rule, and health-watchdog Scheduled Task.
    Does NOT delete data/ or config.yaml (preserved for re-install).
#>

param(
    [string]$ServiceName = "RaidWatch"
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Must run as Administrator."
    exit 1
}

$InstallDir = Split-Path -Parent $PSScriptRoot
$nssm = Join-Path $InstallDir "nssm.exe"

Write-Host "`n=== RaidWatch Service Uninstall ===" -ForegroundColor Cyan

# 1. Stop the service via NSSM (Stop-Service hangs because Python has no console
#    to receive Ctrl+C in service mode; NSSM's stop command handles this better)
Write-Host "Stopping service..." -ForegroundColor Yellow
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if (Test-Path $nssm) {
        & $nssm stop $ServiceName 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 3

    # Check if it actually stopped; force-kill if still running
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') {
        Write-Host "Service didn't stop gracefully, force-killing..." -ForegroundColor Yellow
        Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*$InstallDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Write-Host "Service stopped." -ForegroundColor Green
}

# 2. Remove NSSM service (D18)
Write-Host "Removing NSSM service..." -ForegroundColor Yellow
if (Test-Path $nssm) {
    & $nssm remove $ServiceName confirm 2>&1 | Out-Null
} else {
    sc.exe delete $ServiceName | Out-Null
}
Write-Host "Service removed." -ForegroundColor Green

# 3. Remove firewall rule (D11)
Write-Host "Removing firewall rule..." -ForegroundColor Yellow
Get-NetFirewallRule -DisplayName "RaidWatch" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Firewall rule removed." -ForegroundColor Green

# 4. Remove health watchdog task (D27)
Write-Host "Removing health watchdog task..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName "RaidWatchHealthWatchdog" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Watchdog task removed." -ForegroundColor Green

Write-Host "`n=== Uninstall Complete ===" -ForegroundColor Cyan
Write-Host "Data directory preserved at: $(Join-Path $InstallDir 'data')"
Write-Host "Config preserved: $(Join-Path $InstallDir 'data\config.yaml')"
Write-Host ""
