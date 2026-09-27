' Codex Usage launcher - double-click to start the six-profile widget.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = scriptDir
sh.Run "pythonw """ & scriptDir & "\codex_usage_widget.py""", 0, False
