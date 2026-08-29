# setup_windows.ps1 - prepare Icebreaker Connect on Windows.
#
# Installs the host prerequisites the app cannot fetch itself (mitmproxy, a JDK,
# openssl) via winget, creates a virtualenv, and installs the app into it. The
# Android SDK, emulator and system image are NOT installed here - the app
# downloads those from official Google sources during its "Environment" step
# (adb ships with the platform-tools it installs). The Tinder APK is always
# supplied by you.
#
# The Android emulator needs hardware acceleration on Windows: enable the
# "Windows Hypervisor Platform" optional feature (or install Intel HAXM) and
# reboot once. This script only reminds you; it does not toggle Windows features.
#
# Usage (from a PowerShell prompt in the project root):
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#   ... -NoSystem     # skip winget; only venv + pip install
#   ... -Dev          # also install dev extras (pytest, ruff)
#
# Re-runnable: every step is idempotent.
[CmdletBinding()]
param(
    [switch]$NoSystem,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$Venv  = Join-Path $Root ".venv"
$Extras = if ($Dev) { "[dev]" } else { "" }

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "!  $m"  -ForegroundColor Yellow }
function Die  ($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# -- 1. host prerequisites ----------------------------------------------------
if (-not $NoSystem) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Installing host prerequisites via winget (mitmproxy, JDK, openssl)..."
        # Non-fatal: winget package ids can change; the app's Environment step will
        # report anything still missing.
        $pkgs = @(
            "mitmproxy.mitmproxy",
            "EclipseAdoptium.Temurin.17.JDK",
            "ShiningLight.OpenSSL.Light"
        )
        foreach ($p in $pkgs) {
            winget install --exact --id $p --source winget --silent --disable-interactivity `
                --accept-source-agreements --accept-package-agreements 2>$null
            if ($LASTEXITCODE -ne 0) { Warn "winget could not install $p (install it manually if the check flags it)" }
        }
    } else {
        Warn "winget not found. Install these yourself, then re-run with -NoSystem:"
        Warn "  mitmproxy   a JDK 17+   openssl"
    }
    Warn "Emulator acceleration: enable 'Windows Hypervisor Platform' (optionalfeatures.exe) and reboot once."
}

# -- 2. python check ----------------------------------------------------------
$Python = "python"
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    $Python = "py"
    if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
        Die "Python 3.12+ not found (install from python.org and re-open PowerShell)"
    }
}
$pyver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$pyver -lt [version]"3.12") { Die "Python 3.12+ required, found $pyver" }
Say "Using Python $pyver"

# -- 3. virtualenv + install --------------------------------------------------
if (-not (Test-Path $Venv)) {
    Say "Creating virtualenv at $Venv"
    & $Python -m venv $Venv
}
$VenvPy = Join-Path $Venv "Scripts\python.exe"
Say "Upgrading pip and installing the app (editable$Extras)..."
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -e "$Root$Extras"

# -- 4. readiness report ------------------------------------------------------
Say "Environment readiness:"
& (Join-Path $Venv "Scripts\icebreaker-connect.exe") --check

Write-Host ""
Say "Setup complete."
Write-Host "  Run the GUI:            $Venv\Scripts\icebreaker-connect.exe"
Write-Host "  Or double-click:        scripts\run_windows.bat"
Write-Host "  Re-check the toolchain: $Venv\Scripts\icebreaker-connect.exe --doctor"
Write-Host ""
Write-Host "The app downloads the Android SDK/emulator from Google during its Environment"
Write-Host "step. Supply your own Tinder APK when prompted."
