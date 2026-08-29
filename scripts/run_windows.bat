@echo off
rem run_windows.bat - launch Icebreaker Connect from the project venv.
rem Prefers the venv created by setup_windows.ps1; falls back to `python -m`.
rem Passes through any arguments (e.g. --check, --doctor).
setlocal
set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"

if exist "%VENV%\Scripts\icebreaker-connect.exe" (
    "%VENV%\Scripts\icebreaker-connect.exe" %*
    goto :eof
)
if exist "%VENV%\Scripts\python.exe" (
    "%VENV%\Scripts\python.exe" -m connection_assistant %*
    goto :eof
)
echo No .venv found. Run scripts\setup_windows.ps1 first (or activate your env).
python -m connection_assistant %*
