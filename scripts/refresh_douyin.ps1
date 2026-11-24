# Manual fallback for refreshing the Douyin App session.
#
# Normally the collector agent does this automatically: when the order API
# rejects the session it opens MuMu + the Douyin App, waits for you to log in,
# then captures the fresh WebView cookies. Use this script only when the agent
# is not running or when you want to refresh immediately.
#
# It never opens a browser login: Douyin's order API only accepts the App
# session from the emulator.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$adb = $env:ORDERHUB_ADB
if (-not $adb) { $adb = "F:\Program Files\Netease\MuMuPlayer-12.0\nx_device\12.0\shell\adb.exe" }
$mumu = $env:ORDERHUB_MUMU_MANAGER
if (-not $mumu) { $mumu = "F:\Program Files\Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe" }
$serial = $env:ORDERHUB_ADB_SERIAL
if (-not $serial) { $serial = "127.0.0.1:16384" }

function Test-Adb {
    & $adb -s $serial shell echo ok 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "== 抖音登录态刷新 ==" -ForegroundColor Cyan

if (-not (Test-Adb)) {
    if (Test-Path $mumu) {
        Write-Host "MuMu 未连接，正在启动模拟器..."
        & $mumu control -v 0 launch | Out-Null
        & $mumu control -v 0 show_window | Out-Null
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 3
            if (Test-Adb) { break }
        }
    }
}
if (-not (Test-Adb)) {
    Write-Host "ADB 未就绪：请先手动启动 MuMu，等桌面出现后重试。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "请在模拟器里打开抖音：" -ForegroundColor Yellow
Write-Host "  1. 确认已经登录（掉登录就扫码登录一次）"
Write-Host "  2. 进入抖音首页即可，不需要打开订单页"
Write-Host "  3. 从最近任务里完全退出抖音"
Write-Host ""
Read-Host "完成后按回车，脚本会从模拟器读取会话并验证"

Set-Location $root
& $python -m app.collectors.douyin --refresh
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host ""
    Write-Host "刷新成功：完整 Cookie 已保存，agent 会在下一次采集时使用。" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "刷新失败：确认模拟器里抖音已登录，再重试。" -ForegroundColor Red
}
Read-Host "按回车关闭"
exit $code
