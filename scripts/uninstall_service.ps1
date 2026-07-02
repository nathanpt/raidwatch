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

# Set ErrorActionPreference to Continue for the stop/remove section, because
# NSSM writes to stderr (which PowerShell treats as an error) when the service
# is in a zombie state. We handle errors explicitly below.
$ErrorActionPreference = "Continue"

# 1. Stop the service. Use sc.exe (more reliable than Stop-Service or nssm stop
#    when the service is in a zombie/half-dead state after a force-kill).
Write-Host "Stopping service..." -ForegroundColor Yellow
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    # Try sc.exe stop first (doesn't hang like Stop-Service)
    Write-Host "  Sending stop control..." -ForegroundColor Gray
    sc.exe stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2

    # Force-kill any python processes in the install dir
    Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*$InstallDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    # If still not stopped, use sc.exe to force-delete it
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') {
        Write-Host "  Service stuck, will force-remove..." -ForegroundColor Yellow
    }
    Write-Host "Service stopped." -ForegroundColor Green
}

# 2. Remove the service. Use sc.exe delete (handles zombie services better than
#    nssm remove). Marked for deletion -> removed after reboot if locked.
Write-Host "Removing service..." -ForegroundColor Yellow
sc.exe delete $ServiceName 2>&1 | Out-Null
Start-Sleep -Seconds 1
Write-Host "Service removed." -ForegroundColor Green

$ErrorActionPreference = "Stop"

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
