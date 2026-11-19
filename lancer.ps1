#Requires -Version 5.1
# Auto-DJ — lance le serveur en arrière-plan et ouvre l'UI (Windows).
# Usage : .\lancer.ps1 [-Port 8092] [-Setup]
[CmdletBinding()]
param(
    [int]$Port = 8092,
    [switch]$Setup
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force -Path "data" | Out-Null

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$fresh = -not (Test-Path $py)
if ($fresh) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
    if (-not (Test-Path $py) -and (Get-Command python -ErrorAction SilentlyContinue)) {
        & python -m venv .venv
    }
    if (-not (Test-Path $py)) {
        Write-Host "venv impossible — installez Python 3.10+ : winget install Python.Python.3.12"
        exit 1
    }
}
$ver = "$(& $py -c "import sys; print('%d.%d' % sys.version_info[:2])")".Trim()
if ($ver -notmatch '^\d+\.\d+$' -or [version]$ver -lt [version]"3.10") {
    Write-Host "Python 3.10+ requis (trouvé : $ver) — winget install Python.Python.3.12, puis supprimez .venv et relancez"
    exit 1
}
if ($Setup -or $fresh) {
    & $py -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install a échoué"; exit 1 }
}
& $py -c "import fastapi, numpy, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "fastapi/numpy/uvicorn absents — lancez .\setup.ps1 (ou .\lancer.ps1 -Setup)"
    exit 1
}

$pidFile = Join-Path $PSScriptRoot "data\server.pid"
if (Test-Path $pidFile) {
    $old = "$(Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)".Trim()
    $oldAlive = $old -match '^\d+$' -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)
    if ($oldAlive) {
        $cmd = $null
        try { $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $old").CommandLine } catch { }
        if ($cmd -match "-m\s+app\.server") {
            $runPort = 8092        # sans --port, app.server écoute 8092
            if ($cmd -match "--port\s+(\d+)") { $runPort = [int]$Matches[1] }
            $url = "http://127.0.0.1:$runPort"
            try {
                Invoke-WebRequest -Uri "$url/api/state" -UseBasicParsing -TimeoutSec 2 | Out-Null
            } catch {
                Write-Host "pid $old (app.server) mais /api/state muet sur $runPort — arrêtez-le : .\stop.ps1"
                exit 1
            }
            Write-Host "déjà lancé (pid $old) — $url/"
            Start-Process "$url/" -ErrorAction SilentlyContinue
            exit 0
        }
        if (-not $cmd -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
            Write-Host "pid $old vivant mais cmdline illisible — arrêtez-le ou supprimez data\server.pid"
            exit 1
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

$p = Start-Process -FilePath $py -ArgumentList "-m", "app.server", "--port", "$Port" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $PSScriptRoot "data\server.log") `
    -RedirectStandardError (Join-Path $PSScriptRoot "data\server.err.log")
$p.Id | Out-File -Encoding ascii $pidFile

$url = "http://127.0.0.1:$Port"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "$url/api/state" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    if ($p.HasExited) { break }
    Start-Sleep -Milliseconds 250
}
if (-not $ok) {
    Write-Host "serveur non joignable après 15 s — journal :"
    Get-Content (Join-Path $PSScriptRoot "data\server.log") -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $PSScriptRoot "data\server.err.log") -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue
    Stop-Process -Id $p.Id -ErrorAction SilentlyContinue
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 1
}
Start-Process "$url/" -ErrorAction SilentlyContinue
Write-Host "Auto-DJ en ligne : $url/ (pid $($p.Id))"
