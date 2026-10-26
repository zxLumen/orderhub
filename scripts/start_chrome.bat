@echo off
setlocal
set "PROFILE=%~dp0..\data\chrome-profile"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found. Install Google Chrome first.
  exit /b 1
)
if not exist "%PROFILE%" mkdir "%PROFILE%"
echo Starting Chrome with remote debugging on 127.0.0.1:9222
echo Profile: %PROFILE%
echo Log in to JD / Taobao / Meituan in this window, then leave it running.
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
