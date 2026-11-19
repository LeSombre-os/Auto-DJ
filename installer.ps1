#Requires -Version 5.1
# Auto-DJ — raccourcis Windows : Bureau + menu Démarrer, lancement sans console.
# Usage : powershell -ExecutionPolicy Bypass -File .\installer.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$vbs = Join-Path $root "lancer.vbs"
if (-not (Test-Path -LiteralPath $vbs)) {
    Write-Host "lancer.vbs introuvable dans $root"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root "lancer.ps1"))) {
    Write-Host "lancer.ps1 introuvable dans $root"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root ".venv\Scripts\python.exe"))) {
    Write-Host "note : .venv absent — lancez d'abord : powershell -ExecutionPolicy Bypass -File .\setup.ps1"
}

$icon = Join-Path $root "icon.ico"
$dossiers = @(
    [Environment]::GetFolderPath('Desktop'),
    [Environment]::GetFolderPath('Programs')
)
$shell = New-Object -ComObject WScript.Shell
foreach ($d in $dossiers) {
    if ([string]::IsNullOrEmpty($d)) { continue }
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $lnkPath = Join-Path $d "Auto-DJ.lnk"
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = Join-Path $env:SystemRoot "System32\wscript.exe"
    $lnk.Arguments = "`"$vbs`""
    $lnk.WorkingDirectory = $root
    if (Test-Path -LiteralPath $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Description = "DJ de soirée autonome"
    $lnk.Save()
    Write-Host "raccourci : $lnkPath"
}
Write-Host "terminé — Auto-DJ est sur le Bureau et dans le menu Démarrer (sans fenêtre au lancement)."
Write-Host "désinstallation : supprimer les raccourcis ci-dessus."
exit 0
