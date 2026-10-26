# Idempotent watchdog: make sure OrderHub Chrome + server are running.
# Safe to call at logon and every few minutes.
$ErrorActionPreference = "SilentlyContinue"

$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root "data\autostart.log"
$cdpPort = 9333
$httpPort = 8000

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logPath -Value $line -Encoding utf8
}

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        $client.Close()
        return $true
    } catch {
        $client.Dispose()
        return $false
    }
}

if (-not (Test-Port $cdpPort)) {
    Write-Log "cdp $cdpPort down -> starting headless chrome"
    & (Join-Path $PSScriptRoot "chrome_ctl.ps1") -Mode headless | Out-Null
}

if (-not (Test-Port $httpPort)) {
    Write-Log "http $httpPort down -> starting server"
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    Start-Process -FilePath $python `
        -ArgumentList "-m", "app.server" `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root "data\server.out.log") `
        -RedirectStandardError (Join-Path $root "data\server.err.log") | Out-Null
}
