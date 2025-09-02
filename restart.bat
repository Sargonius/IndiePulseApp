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

REM Stop server if running
if exist .server.pid (
  for /f %%p in (.server.pid) do (
    echo Stopping server PID %%p
    taskkill /PID %%p /F >nul 2>&1
  )
  del /F /Q .server.pid >nul 2>&1
)

REM Stop worker if running
if exist .worker.pid (
  for /f %%p in (.worker.pid) do (
    echo Stopping worker PID %%p
    taskkill /PID %%p /F >nul 2>&1
  )
  del /F /Q .worker.pid >nul 2>&1
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

if not exist logs mkdir logs

REM Ensure the port is free (kill any process listening on %PORT%)
powershell -NoProfile -Command "try { Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } catch { }"

REM Optional autoreload args
set RELOAD_ARGS=
if exist .autoreload set RELOAD_ARGS=--reload --reload-dir app --reload-dir app\templates --reload-dir app\static

REM Start server in a visible window; closing the window will stop the server and free the port
start "IndiePulse Server" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host %HOST% --port %PORT% %RELOAD_ARGS%"

REM Start worker in a visible window
start "IndiePulse Worker" cmd /k ".venv\Scripts\python.exe -m app.worker"

echo Restart complete. Server and worker started in visible windows.
endlocal