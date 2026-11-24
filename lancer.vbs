' Seamless - lance lancer.ps1 sans aucune fenetre (Windows).
' Cible du raccourci Seamless.lnk cree par installer.ps1.
Option Explicit
Dim fso, sh, dir, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(dir & "\lancer.ps1") Then
  MsgBox "lancer.ps1 introuvable dans " & dir, 48, "Seamless"
  WScript.Quit 1
End If
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\lancer.ps1"""
sh.Run cmd, 0, False
