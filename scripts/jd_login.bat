@echo off
rem Visible Chrome (same profile as the collector) for JD login / captcha.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0chrome_ctl.ps1" -Mode visible
