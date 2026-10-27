Option Explicit
Dim shell, files, folder, python, script, code
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = "C:\Program Files\Python311\pythonw.exe"
script = files.BuildPath(folder, "launcher_gui.py")
If Not files.FileExists(python) Then
    MsgBox "Python runtime not found: " & python, 16, "QMT launcher"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
code = shell.Run(Chr(34) & python & Chr(34) & " -X utf8 " & Chr(34) & script & Chr(34), 1, True)
If code <> 0 Then
    MsgBox "Launcher could not start. Exit code: " & code & vbCrLf & "Run launcher_gui.py with python.exe for details.", 16, "QMT launcher"
End If
