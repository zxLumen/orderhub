@echo off
rem Legacy alias: visible Chrome (login / captcha mode).
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0chrome_ctl.ps1" -Mode visible
