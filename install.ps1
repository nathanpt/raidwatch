<#
.SYNOPSIS
    RaidWatch one-command installer.

.DESCRIPTION
    Does EVERYTHING: checks prerequisites, creates the venv, installs deps,
    generates a strong auth token, installs the NSSM service as SYSTEM, creates
    the firewall rule, ACLs the config, registers the health watchdog, and
    starts the service.

    You run ONE command and you're done:

        .\install.ps1

    Optionally pass your LAN subnet so the firewall is scoped correctly:

        .\install.ps1 -LanSubnet "192.168.1.0/24"

.PARAMETER LanSubnet
    Your LAN subnet in CIDR notation (e.g. "192.168.1.0/24").
    Used to scope the firewall rule (D11). Tailscale (100.64.0.0/10) is always included.
    If omitted, you'll be prompted interactively.

.PARAMETER Uninstall
    Run the uninstaller instead (removes service, firewall, watchdog; keeps data).

.EXAMPLE
    .\install.ps1
    .\install.ps1 -LanSubnet "10.0.0.0/8"
    .\install.ps1 -Uninstall
#>

param(
    [string]$LanSubnet = "",
    [string]$ServiceName = "RaidWatch",
    [string]$Port = "8080",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$InstallDir = $PSScriptRoot

#colors
function W-Cyan($t){Write-Host $t -ForegroundColor Cyan}
function W-Yellow($t){Write-Host $t -ForegroundColor Yellow}
function W-Green($t){Write-Host $t -ForegroundColor Green}
function W-Red($t){Write-Host $t -ForegroundColor Red}
function W-Step($n,$t){W-Cyan "`n[$n] $t"}

# -- Uninstall path ----------------------------------------------------------
if ($Uninstall) {
    W-Cyan "`n=== Uninstalling RaidWatch ==="
    & (Join-Path $InstallDir "scripts\uninstall_service.ps1") -ServiceName $ServiceName
    return
}

# -- 0. Verify running as Admin ----------------------------------------------
W-Step "0" "Checking administrator privileges..."
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    W-Red "This installer must be run as Administrator."
    W-Yellow "Right-click PowerShell -> 'Run as Administrator', then re-run:"
    W-Yellow "  .\install.ps1"
    Read-Host "`nPress Enter to exit"
    exit 1
}
W-Green "  OK - running as Administrator."

# -- 1. Check Python 3.12+ ---------------------------------------------------
W-Step "1" "Checking Python 3.12+..."
$pythonCmd = $null
foreach ($cmd in @("python", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (3\.\d+)") {
            $pyVer = [version]$matches[1]
            if ($pyVer -ge [version]"3.12") {
                $pythonCmd = $cmd
                W-Green "  Found $ver"
                break
            }
        }
    } catch {}
}
if (-not $pythonCmd) {
    W-Red "  Python 3.12+ not found."
    W-Yellow "  Download from: https://www.python.org/downloads/"
    W-Yellow "  Install with 'Add Python to PATH' checked, then re-run this script."
    Read-Host "`nPress Enter to exit"
    exit 1
}

# -- 2. Check .NET runtime (needed for temps/LHM; D30) -----------------------
W-Step "2" "Checking .NET runtime (needed for CPU temps)..."
$dotnetOK = $false
try {
    $dotnetRuntimes = & dotnet --list-runtimes 2>&1
    if ($dotnetRuntimes -match "Microsoft\.NETCore\.App|Microsoft\.WindowsDesktop\.App") {
        $dotnetOK = $true
        W-Green "  .NET runtime detected."
    }
} catch {}
if (-not $dotnetOK) {
    W-Yellow "  .NET runtime not found. CPU temps will be unavailable until installed."
    W-Yellow "  (The dashboard works fine without it - temps are optional.)"
    W-Yellow "  Download later: https://dotnet.microsoft.com/download/dotnet/8.0"
}

# -- 3. Create virtualenv + install deps -------------------------------------
W-Step "3" "Setting up Python virtual environment..."
$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    W-Yellow "  Creating .venv..."
    & $pythonCmd -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) { W-Red "  Failed to create venv."; exit 1 }
} else {
    W-Green "  .venv already exists."
}

W-Yellow "  Installing dependencies (this takes a minute)..."
$requirements = Join-Path $InstallDir "requirements.txt"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r $requirements --quiet 2>&1 | ForEach-Object {
    if ($_ -match "error|Error") { W-Red "  $_" }
}
if ($LASTEXITCODE -ne 0) { W-Red "  pip install failed."; exit 1 }
W-Green "  Dependencies installed."

# -- 4. Prompt for LAN subnet (firewall scope; D11) --------------------------
W-Step "4" "Configuring firewall scope..."
if (-not $LanSubnet) {
    W-Cyan "  The firewall rule limits who can reach the dashboard."
    W-Cyan "  Tailscale (100.64.0.0/10) is always included."
    W-Cyan "  Enter your LAN subnet in CIDR notation (e.g. 192.168.1.0/24)."
    W-Cyan "  Press Enter to allow your current network automatically."
    $LanSubnet = Read-Host "  LAN subnet"
    if (-not $LanSubnet) {
        # Auto-detect: find the active IPv4 interface's network
        $ipInfo = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -eq "Dhcp" } | Select-Object -First 1
        if ($ipInfo) {
            $mask = "/" + $ipInfo.PrefixLength
            $network = $ipInfo.IPAddress -replace "\.\d+$", ".0"
            $LanSubnet = "$network$mask"
            W-Green "  Auto-detected: $LanSubnet"
        } else {
            $LanSubnet = "192.168.1.0/24"
            W-Yellow "  Could not auto-detect - using 192.168.1.0/24 (edit later if needed)."
        }
    }
}

# -- 5. First-run: auto-generate config + token (D23/D13) --------------------
W-Step "5" "Generating configuration..."
$configFile = Join-Path $InstallDir "data\config.yaml"
$configExample = Join-Path $InstallDir "config.yaml.example"

# Ensure data dir exists
$dataDir = Join-Path $InstallDir "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }

# Copy example if no config exists
if (-not (Test-Path $configFile)) {
    Copy-Item $configExample $configFile
    W-Green "  Created config.yaml from defaults."
}

# Generate strong token if still placeholder (D13)
$configContent = Get-Content $configFile -Raw
$generatedToken = $null
if ($configContent -match "CHANGE_ME") {
    W-Yellow "  Generating strong auth token..."
    $bytes = New-Object byte[] 32
    ([Security.Cryptography.RandomNumberGenerator]::Create()).GetBytes($bytes)
    $generatedToken = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $configContent = $configContent -replace 'CHANGE_ME[^"]*', $generatedToken
    $configContent | Set-Content $configFile -NoNewline
    W-Green "  Token generated."
} else {
    W-Green "  Token already configured (keeping existing)."
}

# -- 6. Quick foreground test (verify the app starts) ------------------------
W-Step "6" "Verifying the app starts..."
W-Yellow "  Running a 5-second smoke test..."
$mainScript = Join-Path $InstallDir "raidwatch\main.py"
$testJob = Start-Job -ScriptBlock {
    param($py, $script)
    & $py $script 2>&1  # runs once, we'll kill it
} -ArgumentList $venvPython, $mainScript

Start-Sleep -Seconds 5
# The app doesn't exit on its own (it's a server), so we stop the test job
Stop-Job $testJob -ErrorAction SilentlyContinue
$testOutput = Receive-Job $testJob -ErrorAction SilentlyContinue
Remove-Job $testJob -Force -ErrorAction SilentlyContinue

if ($testOutput -match "Traceback|Error|ImportError") {
    W-Red "  The app failed to start:"
    W-Red "  $($testOutput | Select-Object -First 5)"
    W-Yellow "  Check that all dependencies installed correctly."
    exit 1
}
W-Green "  App starts successfully."

# -- 7. Install NSSM service (D18) -------------------------------------------
W-Step "7" "Installing Windows service..."
$nssm = Join-Path $InstallDir "nssm.exe"
if (-not (Test-Path $nssm)) { W-Red "  nssm.exe not found at $nssm"; exit 1 }

# Remove existing service if present
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    W-Yellow "  Stopping existing service (force)..."
    & $nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    # Force-kill any stragglers (Python may not respond to Ctrl+C in service mode)
    Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*$InstallDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    W-Yellow "  Removing existing service..."
    & $nssm remove $ServiceName confirm 2>&1 | Out-Null
}

& $nssm install $ServiceName $venvPython $mainScript
& $nssm set $ServiceName AppDirectory $InstallDir
& $nssm set $ServiceName AppDirectoryBackslash $true
# Run as SYSTEM (needed for LHM kernel driver; D9/D31)
& $nssm set $ServiceName ObjectName LocalSystem
# NSSM captures stdout/stderr + rotation (D26)
& $nssm set $ServiceName AppStdout (Join-Path $InstallDir "data\raidwatch.log")
& $nssm set $ServiceName AppStderr (Join-Path $InstallDir "data\raidwatch.log")
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes 10485760
# SCM recovery: restart on 1st/2nd/3rd failure (D18)
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000
# Stop method: skip Ctrl+C (no console in service mode) and go straight to
# TerminateProcess. Without this, Stop-Service hangs because NSSM waits for a
# Ctrl+C response that the headless Python process can't receive.
& $nssm set $ServiceName AppStopMethodSkip 1
& $nssm set $ServiceName AppStopMethodWindow 2000
W-Green "  Service installed (running as SYSTEM)."

# -- 8. ACL config.yaml to SYSTEM + Administrators (D33) ---------------------
W-Step "8" "Securing config file..."
$acl = Get-Acl $configFile
$acl.SetAccessRuleProtection($true, $false)  # disable inheritance
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("NT AUTHORITY\SYSTEM","FullControl","Allow")))
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("BUILTIN\Administrators","FullControl","Allow")))
Set-Acl $configFile $acl
W-Green "  Config ACL'd to SYSTEM + Administrators only (D33)."

# -- 9. Firewall rule (D11) --------------------------------------------------
W-Step "9" "Creating firewall rule..."
$remoteAddresses = @($LanSubnet, "100.64.0.0/10")
Get-NetFirewallRule -DisplayName "RaidWatch" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "RaidWatch" `
    -Direction Inbound -LocalPort $Port -Protocol TCP `
    -Action Allow -RemoteAddress $remoteAddresses | Out-Null
W-Green "  Firewall scoped to: $($remoteAddresses -join ', ')"

# -- 10. Health watchdog (D27) -----------------------------------------------
W-Step "10" "Registering health watchdog..."
$watchdogScript = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$resp = Invoke-WebRequest -Uri 'http://localhost:$Port/health' -TimeoutSec 10 -UseBasicParsing
if (`$resp.StatusCode -ne 200) {
    Restart-Service -Name '$ServiceName' -Force
}
"@
$watchdogFile = Join-Path $InstallDir "scripts\health_watchdog.ps1"
$watchdogScript | Set-Content $watchdogFile

# Register the watchdog via raw XML to avoid the RepetitionDuration max-value
# limit in New-ScheduledTaskTrigger (36500 days exceeds Task Scheduler's cap).
$taskXml = @"
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
      </Repetition>
      <StartBoundary>$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <AllowStartOnBattery>true</AllowStartOnBattery>
    <DontStopIfGoingOnBatteries>true</DontStopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "$watchdogFile"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName "RaidWatchHealthWatchdog" -Xml $taskXml -Force | Out-Null
W-Green "  Watchdog registered (checks /health every 60s, restarts on failure)."

# -- 11. Start the service ---------------------------------------------------
W-Step "11" "Starting RaidWatch service..."
Start-Service $ServiceName
Start-Sleep -Seconds 3
$svc = Get-Service $ServiceName

# -- Done --------------------------------------------------------------------
W-Cyan "`n==============================================================="
if ($svc.Status -eq 'Running') {
    W-Green "  [OK] RaidWatch is running!"
} else {
    W-Red "  [X] Service status: $($svc.Status) - check data\raidwatch.log"
}

# Display access info
W-Cyan "`n  Access the dashboard:"
W-Yellow "    Local:   http://localhost:$Port"
W-Yellow "    LAN:     http://<this-pc-ip>:$Port"
W-Yellow "    Tailscale: http://<tailscale-ip>:$Port"

# Display token
W-Cyan "`n  Your login token:"
if ($generatedToken) {
    W-Green "    $generatedToken"
    W-Yellow "  Save this! You'll need it to log in."
    W-Yellow "  (Also stored in data\config.yaml)"
} else {
    W-Yellow "    See data\config.yaml -> auth.token"
}

W-Cyan "`n  Optional next steps:"
Write-Host "    * CPU temps:   python scripts\probe_temps.py  (then enable cpu_thermal gate)"
Write-Host "    * Fika setup:   python scripts\discover_processes.py  (fill headless pattern)"
Write-Host "    * Tune gates:   edit data\config.yaml after baselining a raid"
Write-Host ""
Write-Host "  Logs: data\raidwatch.log"
Write-Host "  Uninstall: .\install.ps1 -Uninstall"
W-Cyan "===============================================================`n"
