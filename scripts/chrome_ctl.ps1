param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("visible", "headless", "stop")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$profileDir = Join-Path $root "data\chrome-profile"
$logPath = Join-Path $root "data\chrome.log"
$port = 9333
$cdp = "http://127.0.0.1:$port"

function Write-Log([string]$Message) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Mode, $Message
    Add-Content -Path $logPath -Value $line -Encoding utf8
}

function Find-Chrome {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    throw "Google Chrome not found."
}

function Get-OrderHubChrome {
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -like "*$profileDir*" -or
                $_.CommandLine -like "*--remote-debugging-port=$port*"
            )
        }
}

function Stop-OrderHubChrome {
    $procs = Get-OrderHubChrome
    if (-not $procs) { return }
    Write-Log ("stopping {0} process(es)" -f @($procs).Count)
    foreach ($proc in $procs) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Get-OrderHubChrome)) { break }
        Start-Sleep -Milliseconds 300
    }
}

function Wait-Cdp {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $info = Invoke-RestMethod -Uri "$cdp/json/version" -TimeoutSec 1
            Write-Log ("ready browser={0} ua={1}" -f $info.Browser, $info.'User-Agent')
            Write-Output ("browser={0}" -f $info.Browser)
            Write-Output ("ua={0}" -f $info.'User-Agent')
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    Write-Log "CDP did not come up in time"
    return $false
}

if ($Mode -eq "stop") {
    Stop-OrderHubChrome
    Write-Log "stopped"
    exit 0
}

Stop-OrderHubChrome

if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}

$chrome = Find-Chrome
$parts = @(
    "--remote-debugging-port=$port",
    "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=`"$profileDir`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1440,900"
)
if ($Mode -eq "headless") {
    # Chrome advertises HeadlessChrome in the UA, which is a detection signal.
    $version = (Get-Item $chrome).VersionInfo.ProductVersion
    $major = ($version -split "\.")[0]
    $ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
          "(KHTML, like Gecko) Chrome/$major.0.0.0 Safari/537.36"
    $parts = @("--headless=new", "--user-agent=`"$ua`"") + $parts
}
$parts += "about:blank"
$argLine = $parts -join " "

Write-Log ("starting {0} mode ({1})" -f $Mode, $chrome)
Start-Process -FilePath $chrome -ArgumentList $argLine | Out-Null
if (-not (Wait-Cdp)) { exit 1 }
