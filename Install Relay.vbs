Option Explicit
Dim fs, sh, root, runner, choice, n
Set fs=CreateObject("Scripting.FileSystemObject")
Set sh=CreateObject("WScript.Shell")
root=fs.GetParentFolderName(WScript.ScriptFullName)
runner=""
For Each choice In Array("pyw.exe -3","pythonw.exe")
  On Error Resume Next
  Err.Clear
  n=sh.Run(choice & " -c ""import sys;sys.exit(sys.version_info < (3,9))""",0,True)
  If Err.Number=0 And n=0 Then runner=choice
  On Error GoTo 0
  If runner<>"" Then Exit For
Next
If runner="" Then
  MsgBox "Install Python 3.9+ for Windows, then run Install Relay again.",vbInformation,"Drive Relay"
  WScript.Quit 2
End If
n=sh.Run(runner & " """ & fs.BuildPath(root,"install_desktop.py") & """ --open",0,True)
If n<>0 Then MsgBox "Installation failed. Check the Python installation and your application directory.",vbExclamation,"Drive Relay"
WScript.Quit n
