@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv. Ensure Python 3 is installed and on PATH.
    exit /b 1
  )
)

echo Installing/Updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

set HOST=127.0.0.1
set PORT=8000

if exist .env (
  for /f "tokens=1,2 delims==" %%G in (.env) do (
    if /I "%%G"=="APP_HOST" set HOST=%%H
    if /I "%%G"=="APP_PORT" set PORT=%%H
  )
)

REM Free the server port if something is listening
powershell -NoProfile -Command "try { Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } catch { }"

REM Optional autoreload
set RELOAD_ARGS=
if exist .autoreload set RELOAD_ARGS=--reload --reload-dir app --reload-dir app\templates --reload-dir app\static

REM Launch server in a visible window; closing it frees the port
start "IndiePulse Server" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host %HOST% --port %PORT% %RELOAD_ARGS%"
REM Launch worker in a visible window
start "IndiePulse Worker" cmd /k ".venv\Scripts\python.exe -m app.worker"
endlocal
