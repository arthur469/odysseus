$ErrorActionPreference = 'Stop'
$appRoot = Join-Path $env:LOCALAPPDATA 'Programs\Odysseus'
Set-Location -LiteralPath $appRoot
$pythonExe = Join-Path $appRoot 'venv\Scripts\python.exe'
$env:APP_PORT = '7000'
$env:APP_BIND = '0.0.0.0'
$env:PYTHONIOENCODING = 'utf-8'
$env:ANONYMIZED_TELEMETRY = 'false'
$env:PATH = "$appRoot\venv\Scripts;$appRoot\venv\Lib\site-packages\torch\lib;C:\Program Files\LibreOffice\program;$appRoot\tools\node-v24.22.1-win-x64;$env:LOCALAPPDATA\Programs\Ollama;C:\Program Files\Git\bin;" + $env:PATH
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker start odysseus-chroma 2>$null | Out-Null
}
if (-not (Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput "$appRoot\logs\ollama.out.log" -RedirectStandardError "$appRoot\logs\ollama.err.log"
}
# Relais local des routines personnelles (bibliotheque externe)
& (Join-Path $env:USERPROFILE 'Documents\Automatisations-PC\scripts\Demarrer-Routeur.ps1')
$listener = Get-NetTCPConnection -State Listen -LocalPort 7000 -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listener) {
    Start-Process -FilePath $pythonExe -ArgumentList '-m','uvicorn','app:app','--host','0.0.0.0','--port','7000' -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput "$appRoot\logs\server.out.log" -RedirectStandardError "$appRoot\logs\server.err.log" | Out-Null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort 7000 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $listener) { throw 'Odysseus did not start; see logs\server.err.log.' }
}
$serverInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$knownPython = @($pythonExe, "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
if ($serverInfo.ExecutablePath -notin $knownPython -or $serverInfo.CommandLine -notmatch '-m uvicorn app:app .*--port 7000') {
    throw 'Port 7000 is occupied by another application.'
}
$listener.OwningProcess | Set-Content -LiteralPath "$appRoot\data\server.pid"
