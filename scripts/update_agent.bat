@echo off
setlocal
cd /d "%~dp0.."

echo [1/4] pulling latest code...
git pull --ff-only
if errorlevel 1 goto :error

echo [2/4] installing collector dependencies...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip install -i https://pypi.org/simple -r requirements-jd.txt
) else (
  python -m pip install -i https://pypi.org/simple -r requirements-jd.txt
)
if errorlevel 1 goto :error

echo [3/4] stopping old agent...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*app.collector_agent*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

echo [4/4] starting agent...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure_running.ps1"
if errorlevel 1 goto :error

echo.
echo Done. Agent version:
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "from app.collector_agent import VERSION; print(VERSION)"
) else (
  python -c "from app.collector_agent import VERSION; print(VERSION)"
)
pause
exit /b 0

:error
echo.
echo Update failed. Fix the error above and run again.
pause
exit /b 1
