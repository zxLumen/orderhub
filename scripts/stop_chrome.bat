@echo off
rem Stop only the OrderHub Chrome (never the everyday Chrome).
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0chrome_ctl.ps1" -Mode stop
