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

REM Free the port if occupied (best-effort) to avoid bind errors in debug
powershell -NoProfile -Command "$port=%PORT%; $conns=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if($conns){ $pids=($conns | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique); foreach($procId in $pids){ try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } catch {} } }"

echo Starting IndiePulse at %HOST%:%PORT% (visible console)...
echo Press Ctrl+C to stop.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT%
echo Uvicorn exited with code %errorlevel%
endlocal


