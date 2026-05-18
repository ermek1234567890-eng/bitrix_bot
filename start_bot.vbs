Dim botDir, WShell
Set WShell = CreateObject("WScript.Shell")
botDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
WShell.Run "pythonw """ & botDir & "bot.py""", 0, False
