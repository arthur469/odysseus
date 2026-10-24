& "$PSScriptRoot\Start-Odysseus.ps1"
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $null = Invoke-WebRequest -Uri 'http://127.0.0.1:7000/login' -UseBasicParsing -TimeoutSec 2
        $ready = $true
        break
    } catch { Start-Sleep -Seconds 1 }
}
if ($ready) { Start-Process 'http://127.0.0.1:7000' }
else { Write-Error 'Odysseus is not responding. See the logs folder.' }
