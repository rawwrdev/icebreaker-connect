' run_windows.vbs - launch Icebreaker Connect with NO console window.
' Double-click this file (or pin a shortcut to it). It runs the venv's GUI entry
' point via WScript so no terminal is shown at all.
Option Explicit
Dim fso, shell, root, guiExe, pythonw
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Project root = parent of this script's folder.
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

guiExe  = root & "\.venv\Scripts\icebreaker-connect-gui.exe"
pythonw = root & "\.venv\Scripts\pythonw.exe"

If fso.FileExists(guiExe) Then
    shell.Run """" & guiExe & """", 0, False
ElseIf fso.FileExists(pythonw) Then
    shell.Run """" & pythonw & """ -m connection_assistant", 0, False
Else
    MsgBox "No .venv found. Run scripts\setup_windows.ps1 first.", vbExclamation, "Icebreaker Connect"
End If
