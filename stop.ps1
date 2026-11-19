#Requires -Version 5.1
# Auto-DJ — arrête proprement le serveur (pid file, Stop-Process ciblé).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot "data\server.pid"
if (-not (Test-Path $pidFile)) { Write-Host "pas de data\server.pid"; exit 0 }
$serverPid = "$(Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)".Trim()
if ($serverPid -notmatch '^\d+$') {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "pid file vide ou invalide"
    exit 0
}
$serverPid = [int]$serverPid
if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "pid $serverPid déjà mort"
    exit 0
}
# filet : ne tuer que si c'est bien notre serveur (cmdline via CIM)
$cmd = $null
try {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid").CommandLine
} catch { }
if (-not $cmd) {
    Write-Host "pid $serverPid : cmdline illisible — pas tué ; pid file supprimé"
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 1
}
if ($cmd -notmatch "-m\s+app\.server") {
    Write-Host "pid $serverPid : cmdline sans '-m app.server' — pas tué ; pid file supprimé"
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}
Stop-Process -Id $serverPid -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
    Write-Host "pid $serverPid résiste — pid file conservé (data\server.pid)"
    exit 1
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "arrêté (pid $serverPid)"
