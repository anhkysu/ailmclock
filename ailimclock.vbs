' ailimclock launcher - double-click to start the widget with NO console window.
' Runs pythonw (windowless Python) hidden, from this script's own folder.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = scriptDir
' window style 0 = hidden, False = don't wait for it to exit
sh.Run "pythonw """ & scriptDir & "\ailimclock_widget.py""", 0, False
