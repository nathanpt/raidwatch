<#
.SYNOPSIS
    Install RaidWatch as a Windows service using vendored NSSM (D18).

.DESCRIPTION
    Installs RaidWatch as a SYSTEM service via NSSM with:
    - SCM recovery on 1st/2nd/3rd failure (D18)
    - Firewall rule scoped to LAN subnet + Tailscale (D11)
    - ACL on config.yaml: SYSTEM + Administrators only (D33)
    - External /health watchdog Scheduled Task for native-hang backstop (D27)

.NOTES
    Run as Administrator. Requires the .NET runtime (D30) and the vendored
    nssm.exe at the repo root.
#>

param(
    [string]$ServiceName = "RaidWatch",
    [string]$InstallDir = "D:\Tools\RaidWatch",
    [string[]]$LanSubnets = @("192.168.1.0/24"),  # EDIT: your LAN subnet(s)
    [string]$TailscaleRange = "100.64.0.0/10"
)

$ErrorActionPreference = "Stop"

# --- Verify admin ---
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Must run as Administrator."
    exit 1
}

$nssm = Join-Path $InstallDir "nssm.exe"
$pythonExe = Join-Path $InstallDir ".venv\Scripts\python.exe"
$mainScript = Join-Path $InstallDir "raidwatch\main.py"
$configFile = Join-Path $InstallDir "data\config.yaml"

Write-Host "`n=== RaidWatch Service Install (D18/D27/D33) ===" -ForegroundColor Cyan

# --- 0. Verify prerequisites ---
if (-not (Test-Path $nssm)) { Write-Error "nssm.exe not found at $nssm"; exit 1 }
if (-not (Test-Path $pythonExe)) { Write-Error "Python venv not found at $pythonExe"; exit 1 }

# --- 1. Generate strong auth token (D13) ---
# Check if config exists; if the token is still the placeholder, generate a new one
if (Test-Path $configFile) {
    $configContent = Get-Content $configFile -Raw
    if ($configContent -match "CHANGE_ME") {
        Write-Host "Generating strong auth token (D13)..." -ForegroundColor Yellow
        $bytes = New-Object byte[] 32
        ([Security.Cryptography.RandomNumberGenerator]::Create()).GetBytes($bytes)
        $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
        $configContent = $configContent -replace "CHANGE_ME[^""]*", $token
        # Write back via Python to preserve YAML formatting
        $configContent | Set-Content $configFile -NoNewline
        Write-Host "Token generated. Save it — you'll need it to log in." -ForegroundColor Green
        Write-Host "Token: $token" -ForegroundColor Yellow
    }
} else {
    Write-Warning "Config not found at $configFile — will auto-generate on first run (D23)."
}

# --- 2. Install NSSM service (D18) ---
Write-Host "`nInstalling NSSM service '$ServiceName' as SYSTEM (D9/D18)..." -ForegroundColor Yellow
& $nssm install $ServiceName $pythonExe $mainScript
& $nssm set $ServiceName AppDirectory $InstallDir
& $nssm set $ServiceName ObjectName LocalSystem
# NSSM captures stdout/stderr + rotation (D26)
& $nssm set $ServiceName AppStdout (Join-Path $InstallDir "data\raidwatch.log")
& $nssm set $ServiceName AppStderr (Join-Path $InstallDir "data\raidwatch.log")
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes 10485760  # 10MB

# SCM recovery: restart on 1st/2nd/3rd failure (D18)
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000

Write-Host "Service installed." -ForegroundColor Green

# --- 3. ACL config.yaml to SYSTEM + Administrators (D33) ---
if (Test-Path $configFile) {
    Write-Host "`nACLing config.yaml to SYSTEM + Administrators (D33)..." -ForegroundColor Yellow
    $acl = Get-Acl $configFile
    $acl.SetAccessRuleProtection($true, $false)  # disable inheritance
    $ruleSystem = New-Object Security.AccessControl.FileSystemAccessRule("NT AUTHORITY\SYSTEM","FullControl","Allow")
    $ruleAdmins = New-Object Security.AccessControl.FileSystemAccessRule("BUILTIN\Administrators","FullControl","Allow")
    $acl.AddAccessRule($ruleSystem)
    $acl.AddAccessRule($ruleAdmins)
    Set-Acl $configFile $acl
    Write-Host "ACL applied." -ForegroundColor Green
}

# --- 4. Firewall rule (D11) ---
Write-Host "`nCreating firewall rule (LAN + Tailscale, excluding guest/IoT; D11)..." -ForegroundColor Yellow
$remoteAddresses = @($LanSubnets + $TailscaleRange) -join ","
$existingRule = Get-NetFirewallRule -DisplayName "RaidWatch" -ErrorAction SilentlyContinue
if ($existingRule) { Remove-NetFirewallRule -DisplayName "RaidWatch" }
New-NetFirewallRule -DisplayName "RaidWatch" `
    -Direction Inbound -LocalPort 8080 -Protocol TCP `
    -Action Allow -RemoteAddress $remoteAddresses | Out-Null
Write-Host "Firewall rule created (scoped to: $remoteAddresses)." -ForegroundColor Green

# --- 5. External /health watchdog Scheduled Task (D27) ---
# This is the irreducible native-hang backstop: curls /health on a short timeout
# and restarts the service if it fails (NSSM can't catch a hung-but-alive process).
Write-Host "`nRegistering external /health watchdog (D27)..." -ForegroundColor Yellow
$watchdogScript = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$resp = Invoke-WebRequest -Uri 'http://localhost:8080/health' -TimeoutSec 10 -UseBasicParsing
if (`$resp.StatusCode -ne 200) {
    Restart-Service -Name '$ServiceName' -Force
    Write-EventLog -LogName Application -Source 'RaidWatch' -EntryType Error -EventId 1 -Message 'Health check failed — service restarted'
}
"@
$watchdogFile = Join-Path $InstallDir "scripts\health_watchdog.ps1"
$watchdogScript | Set-Content $watchdogFile

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdogFile`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 36500)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "RaidWatchHealthWatchdog" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Watchdog task registered (runs every 1 min)." -ForegroundColor Green

# --- 6. Start the service ---
Write-Host "`nStarting service..." -ForegroundColor Yellow
Start-Service $ServiceName
Start-Sleep -Seconds 3
$svc = Get-Service $ServiceName
Write-Host "Service status: $($svc.Status)" -ForegroundColor $(if ($svc.Status -eq 'Running') {'Green'} else {'Red'})

Write-Host "`n=== Installation Complete ===" -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:8080 (LAN) or http://<tailscale-ip>:8080"
Write-Host "Login with the token from data/config.yaml"
Write-Host "`nNext steps:"
Write-Host "  9.  Run probe_temps.py to validate CPU temp sensor (D9)"
Write-Host "  10. Run discover_processes.py to fill headless pattern (D4)"
Write-Host "  11. Baseline a raid, then tune gate thresholds (D10)"
Write-Host ""
