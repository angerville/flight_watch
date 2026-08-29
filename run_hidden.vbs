' run_hidden.vbs - run a batch file with no visible console window.
'
' Task Scheduler launching a .bat directly (LogonType=Interactive) flashes an
' empty cmd window on every run. Going through this wrapper runs it with window
' style 0 (hidden) so nothing appears.
'
' Usage:  wscript.exe run_hidden.vbs "<full path to the .bat>"
'
' Run(cmd, 0, True): 0 = hidden window, True = wait until the batch exits.
' Waiting lets Task Scheduler track run time / MultipleInstances / exit code.
'
' Keep this file ASCII-only and WITHOUT a BOM. The VBScript engine rejects a
' UTF-8 BOM as an invalid character (compile error 800A0408).
Option Explicit
Dim sh, rc
If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If
Set sh = CreateObject("WScript.Shell")
rc = sh.Run("""" & WScript.Arguments(0) & """", 0, True)
WScript.Quit rc
