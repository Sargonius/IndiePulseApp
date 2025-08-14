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

REM Write server PID to .server.pid for restart script
powershell -NoProfile -Command "$p = Start-Process -FilePath '.\\.venv\\Scripts\\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','%HOST%','--port','%PORT%' -PassThru; Set-Content -Path '.server.pid' -Value $p.Id"
endlocal
