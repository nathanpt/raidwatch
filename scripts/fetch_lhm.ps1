<#
.SYNOPSIS
  Fetch and vendor the LibreHardwareMonitor DLL set (D30).

.DESCRIPTION
  Downloads the PINNED official LibreHardwareMonitor release, verifies the
  asset SHA-256, and extracts the library DLL subset into vendor/lhm/.
  Also fetches the MPL-2.0 LICENSE for attribution (D30).

  This is a reproducibility / upgrade tool: the DLLs are already committed in
  vendor/lhm/. Re-run it only when upgrading the pinned version.

.NOTES
  Official-release-only (D31): never point this at a third-party mirror.
  Uses the .NET Framework build (see vendor/lhm/README.md for rationale).
#>

param(
    [string]$TargetDir = (Join-Path $PSScriptRoot ".." "vendor" "lhm")
)

# --- Pinned release ----------------------------------------------------------
$LhmVersion = "v0.9.6"
$AssetName  = "LibreHardwareMonitor.zip"
$AssetHash  = "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"
$BaseUrl    = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$LhmVersion"
$LicenseUrl = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/$LhmVersion/LICENSE"

# DLLs to EXCLUDE (UI/chart app-only; never referenced by the headless library).
$ExcludeDlls = @(
    "LibreHardwareMonitor.exe",
    "LibreHardwareMonitor.exe.config",
    "Aga.Controls.dll",
    "OxyPlot.dll",
    "OxyPlot.WindowsForms.dll"
)

$TargetDir = (Resolve-Path -LiteralPath (New-Item -ItemType Directory -Force -Path $TargetDir)).Path
Write-Host "Target:  $TargetDir"
Write-Host "Release: LibreHardwareMonitor $LhmVersion (.NET Framework build)"

# --- Download ----------------------------------------------------------------
$zipPath = Join-Path $env:TEMP "raidwatch_lhm_$LhmVersion.zip"
$zipUrl  = "$BaseUrl/$AssetName"
Write-Host "Download: $zipUrl"
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

# --- Verify SHA-256 ----------------------------------------------------------
$actual = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLower()
Write-Host "SHA-256:  $actual"
if ($actual -ne $AssetHash.ToLower()) {
    Write-Error "SHA-256 mismatch! Expected $AssetHash."
    Remove-Item $zipPath -Force
    exit 1
}
Write-Host "SHA-256 OK." -ForegroundColor Green

# --- Extract filtered subset -------------------------------------------------
$staging = Join-Path $env:TEMP "raidwatch_lhm_staging"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $staging -Force

# Clear existing managed DLLs + LICENSE in target (keep README.md).
Get-ChildItem -Path $TargetDir -File | Where-Object { $_.Name -ne "README.md" } | Remove-Item -Force

$copied = 0
Get-ChildItem -Path $staging -File -Filter "*.dll" | Where-Object {
    $ExcludeDlls -notcontains $_.Name
} | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $TargetDir -Force
    $copied++
}

# --- LICENSE (D30 attribution) ----------------------------------------------
Invoke-WebRequest -Uri $LicenseUrl -OutFile (Join-Path $TargetDir "LICENSE.txt") -UseBasicParsing

# --- Cleanup -----------------------------------------------------------------
Remove-Item $zipPath -Force
Remove-Item $staging -Recurse -Force

Write-Host ""
Write-Host "Done: $copied DLLs + LICENSE.txt vendored into $TargetDir" -ForegroundColor Green
Write-Host "Next: bump the version + checksum in this script and vendor/lhm/README.md on upgrade." -ForegroundColor DarkGray
