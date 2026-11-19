#Requires -Version 5.1
# Auto-DJ — crée .venv et installe les dépendances (à lancer une fois, Windows).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
    if (-not (Test-Path $py) -and (Get-Command python -ErrorAction SilentlyContinue)) {
        & python -m venv .venv
    }
    if (-not (Test-Path $py)) {
        Write-Host "Python introuvable — installation : winget install Python.Python.3.12"
        exit 1
    }
}
$ver = "$(& $py -c "import sys; print('%d.%d' % sys.version_info[:2])")".Trim()
if ($ver -notmatch '^\d+\.\d+$' -or [version]$ver -lt [version]"3.10") {
    Write-Host "Python 3.10+ requis (trouvé : $ver) — winget install Python.Python.3.12, puis supprimez .venv et relancez"
    exit 1
}
& $py -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "pip install a échoué"; exit 1 }
$missing = @()
foreach ($b in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $b -ErrorAction SilentlyContinue)) { $missing += $b }
}
if ($missing.Count -gt 0) {
    Write-Host "$($missing -join ', ') absent(s) — installation : winget install Gyan.FFmpeg"
}
& $py -c "import sounddevice" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "sounddevice indisponible — réinstallez les dépendances : $py -m pip install -r requirements.txt"
}
Write-Host "ok — lancez .\lancer.ps1"
