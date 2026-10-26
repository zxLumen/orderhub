@echo off
rem Headless Chrome for daily collection: no window, no taskbar entry.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0chrome_ctl.ps1" -Mode headless
