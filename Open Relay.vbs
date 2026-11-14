Option Explicit
Dim fs, shell, root, runner, candidate, exitCode, checkOnly
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fs.GetParentFolderName(WScript.ScriptFullName)
checkOnly = False
If WScript.Arguments.Count > 0 Then checkOnly = (WScript.Arguments(0) = "--check")
runner = ""
For Each candidate In Array("pyw.exe -3", "pythonw.exe")
    On Error Resume Next
    Err.Clear
    exitCode = shell.Run(candidate & " -c ""import sys;sys.exit(sys.version_info < (3,9))""", 0, True)
    If Err.Number = 0 And exitCode = 0 Then runner = candidate
    On Error GoTo 0
    If runner <> "" Then Exit For
Next
If runner = "" Then
    If Not checkOnly Then MsgBox "Install Python 3.9+ for Windows (including pythonw), then open this file again." & vbCrLf & "https://www.python.org/downloads/windows/", vbInformation, "Drive SSH Relay"
    WScript.Quit 2
End If
If Not checkOnly Then shell.Run runner & " """ & fs.BuildPath(root,"relay.py") & """ panel --open", 0, False
