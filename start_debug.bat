@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

set HOST=127.0.0.1
set PORT=8000
if exist .env (
  for /f "tokens=1,2 delims==" %%G in (.env) do (
    if /I "%%G"=="APP_HOST" set HOST=%%H
    if /I "%%G"=="APP_PORT" set PORT=%%H
  )
)

echo Starting IndiePulse at %HOST%:%PORT% (foreground, logs to console)...
echo Press Ctrl+C to stop. Full server logs also in logs\app.log

".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT%
echo Uvicorn exited with code %errorlevel%
pause
endlocal


