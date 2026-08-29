# install_shortcut.ps1 - add a Start Menu shortcut that opens the GUI (no console).
#
# The shortcut targets the console-less GUI launcher (icebreaker-connect-gui.exe),
# so clicking it from the Start Menu opens the window with no terminal. Run
# scripts\setup_windows.ps1 first so the venv exists.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\install_shortcut.ps1
#   ... -Uninstall
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$StartMenu = [Environment]::GetFolderPath("Programs")
$LinkPath = Join-Path $StartMenu "Icebreaker Connect.lnk"

if ($Uninstall) {
    if (Test-Path $LinkPath) { Remove-Item $LinkPath; Write-Host "Removed $LinkPath" }
    exit 0
}

$GuiExe  = Join-Path $Root ".venv\Scripts\icebreaker-connect-gui.exe"
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($LinkPath)
if (Test-Path $GuiExe) {
    $sc.TargetPath = $GuiExe
} elseif (Test-Path $Pythonw) {
    $sc.TargetPath = $Pythonw
    $sc.Arguments = "-m connection_assistant"
} else {
    throw "No .venv found. Run scripts\setup_windows.ps1 first."
}
$sc.WorkingDirectory = $Root
$sc.Description = "Icebreaker Connect"
$sc.Save()
Write-Host "Installed Start Menu shortcut: $LinkPath"
