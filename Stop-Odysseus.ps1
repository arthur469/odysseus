$appRoot = Join-Path $env:LOCALAPPDATA 'Programs\Odysseus'
$pidFile = Join-Path $appRoot 'data\server.pid'
if (Test-Path -LiteralPath $pidFile) {
    $serverPid = [int](Get-Content -LiteralPath $pidFile)
    $serverInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
    $knownPython = @("$appRoot\venv\Scripts\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
    if ($serverInfo.ExecutablePath -in $knownPython -and $serverInfo.CommandLine -match '-m uvicorn app:app .*--port 7000') {
        Stop-Process -Id $serverPid
    }
}
if (Get-Command docker -ErrorAction SilentlyContinue) { docker stop odysseus-chroma | Out-Null }

# Relais local des routines personnelles
& (Join-Path $env:USERPROFILE 'Documents\Automatisations-PC\scripts\Arreter-Routeur.ps1')
