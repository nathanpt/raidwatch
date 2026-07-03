<#
.SYNOPSIS
    Reliably restart the RaidWatch service, recovering from stuck StopPending.

.DESCRIPTION
    NSSM's graceful stop can hang for headless Python services (no console to
    receive Ctrl+C), leaving the service in StopPending forever. This script
    never relies on graceful stop:

      * It finds the service's host (nssm.exe) PID via WMI (Win32_Service).
      * It force-kills the monitored python.exe child, matched by PARENT PID
        (deterministic -- no fragile command-line string matching).
      * If the service was Running, NSSM's AppExit=Restart relaunches it with
        fresh code automatically.
      * If it was wedged in StopPending, the kill lets NSSM complete the stop;
        if even that stalls, the host PID is killed so SCM clears, then we
        start fresh.

    Run from an elevated PowerShell. Safe to run any time.

.PARAMETER ServiceName
    NSSM service name. Default: RaidWatch.

.EXAMPLE
    .\scripts\restart_service.ps1
#>
param([string]$ServiceName = "RaidWatch")

$ErrorActionPreference = "Continue"

function SvcState {
    (Get-Service $ServiceName -ErrorAction SilentlyContinue).Status
}
function SvcHostPid {
    (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue).ProcessId
}
function Kill-AppChild {
    # Kill python.exe whose parent is this service's nssm host. Parent-PID
    # matching is robust regardless of how the cmdline looks or who owns it.
    $h = SvcHostPid
    if (-not $h) { return }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $h } |
        ForEach-Object {
            Write-Host "    killing app PID $($_.ProcessId)" -ForegroundColor Yellow
            taskkill /F /PID $_.ProcessId 2>$null | Out-Null
        }
}

$initial  = SvcState
$hostPid  = SvcHostPid
Write-Host "==> $ServiceName : $initial (host PID $hostPid)" -ForegroundColor Cyan

if ($initial -eq "Running") {
    # --- Clean restart: kill the app child; NSSM AppExit-restarts it fresh ---
    Write-Host "==> Killing app process; NSSM will relaunch with fresh code..." -ForegroundColor Cyan
    Kill-AppChild
    $ok = $false
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Seconds 1
        if ((SvcState) -eq "Running") { $ok = $true; break }
    }
    if (-not $ok) {
        Write-Host "==> NSSM did not relaunch; starting explicitly..." -ForegroundColor Yellow
        sc.exe start $ServiceName 2>$null | Out-Null
        Start-Sleep -Seconds 4
    }
}
else {
    # --- Wedged (StopPending) or Stopped: drive to Stopped, then start -------
    Write-Host "==> Driving to Stopped..." -ForegroundColor Cyan
    sc.exe stop $ServiceName 2>$null | Out-Null
    Start-Sleep -Seconds 1
    Kill-AppChild
    $stopped = $false
    for ($i = 0; $i -lt 10; $i++) {
        if ((SvcState) -eq "Stopped") { $stopped = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $stopped -and $hostPid) {
        Write-Host "==> Still wedged; killing host PID $hostPid..." -ForegroundColor Yellow
        taskkill /F /PID $hostPid 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }
    Write-Host "==> Starting..." -ForegroundColor Cyan
    sc.exe start $ServiceName 2>$null | Out-Null
    Start-Sleep -Seconds 4
}

if ((SvcState) -eq "Running") {
    Write-Host "[OK] $ServiceName is Running." -ForegroundColor Green
}
else {
    Write-Host "[X] $ServiceName status: $(SvcState)" -ForegroundColor Red
}
Get-Service $ServiceName
