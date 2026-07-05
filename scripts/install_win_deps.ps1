<#
.SYNOPSIS
    Install RaidWatch's Windows-only Python deps into the venv and restart.

.DESCRIPTION
    The shipped requirements.txt was generated on Linux, so it omits the
    platform-conditional Windows deps (pywin32, pythonnet). A production
    install therefore silently lacks them and every pywin32 metric (WHEA,
    disk queue, pages/sec) plus CPU temps come back null. This script
    installs both into the existing venv, verifies the imports, runs
    pywin32's postinstall only if an import fails, then restarts the service.

    Run from an elevated PowerShell.

.EXAMPLE
    .\scripts\install_win_deps.ps1
#>
$ErrorActionPreference = "Stop"

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "venv python not found at $venvPython -- run .\install.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "=== Installing Windows-only deps ===" -ForegroundColor Cyan
Write-Host "python : $venvPython`n"

Write-Host "--> pip install pywin32 pythonnet ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pywin32 pythonnet 2>&1 | ForEach-Object {
    if ($_ -match 'error|Error|Successfully installed|already satisfied|Requirement already') { Write-Host "    $_" }
}
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 }

function Test-Import($mod) {
    & $venvPython -c "import $mod" 2>$null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "`n--> verifying imports ..." -ForegroundColor Cyan
$evtlog = Test-Import win32evtlog
$pdh    = Test-Import win32pdh
$clr    = Test-Import clr
Write-Host ("    win32evtlog : {0}" -f $(if ($evtlog) {'ok'} else {'FAILED'}))
Write-Host ("    win32pdh    : {0}" -f $(if ($pdh) {'ok'} else {'FAILED'}))
Write-Host ("    clr (pythonnet) : {0}" -f $(if ($clr) {'ok'} else {'FAILED'}))

# pywin32's DLLs usually register via its bootstrap .pth, but some setups need
# the explicit postinstall (copies pywintypes/pythoncom DLLs). Only run it if
# an import failed.
if (-not $evtlog -or -not $pdh) {
    Write-Host "`n--> pywin32 import failed; running postinstall ..." -ForegroundColor Yellow
    $post = Join-Path $RepoRoot ".venv\Scripts\pywin32_postinstall.py"
    if (Test-Path $post) {
        & $venvPython $post -install 2>&1 | ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "    pywin32_postinstall.py not found at $post" -ForegroundColor Red
    }
    $evtlog = Test-Import win32evtlog
    $pdh    = Test-Import win32pdh
    Write-Host ("    retry win32evtlog : {0}" -f $(if ($evtlog) {'ok'} else {'FAILED'}))
    Write-Host ("    retry win32pdh    : {0}" -f $(if ($pdh) {'ok'} else {'FAILED'}))
}

Write-Host "`n--> restarting RaidWatch service ..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "restart_service.ps1")

Write-Host "`n=== done ===" -ForegroundColor Cyan
if ($evtlog -and $pdh) {
    Write-Host "pywin32 OK -- run .\scripts\validate_windows.ps1 to confirm WHEA + counters are live." -ForegroundColor Green
} else {
    Write-Host "pywin32 still failing after postinstall -- paste this output back." -ForegroundColor Red
}
if (-not $clr) {
    Write-Host "pythonnet (clr) FAILED -- CPU temps need it; run this script again after installing pythonnet." -ForegroundColor Yellow
} else {
    Write-Host "pythonnet OK -- LHM DLLs are vendored in vendor\lhm\. Run scripts\probe_temps.py to validate the sensor." -ForegroundColor Green
}
