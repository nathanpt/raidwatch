<#
.SYNOPSIS
    Uninstall the RaidWatch Windows service and clean up (reverse of install_service.ps1).

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

# 1. Stop the service
Write-Host "Stopping service..." -ForegroundColor Yellow
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Stop-Service $ServiceName -Force
    Start-Sleep -Seconds 2
    Write-Host "Service stopped." -ForegroundColor Green
}

# 2. Remove NSSM service (D18)
Write-Host "Removing NSSM service..." -ForegroundColor Yellow
if (Test-Path $nssm) {
    & $nssm remove $ServiceName confirm
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
