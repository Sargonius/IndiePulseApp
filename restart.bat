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

REM Start server in background and save PID (hidden window), logs redirected
powershell -NoProfile -Command "$args = @('-m','uvicorn','app.main:app','--host','%HOST%','--port','%PORT%'); $p = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList $args -WorkingDirectory '.' -WindowStyle Hidden -RedirectStandardOutput 'logs/server.out.log' -RedirectStandardError 'logs/server.err.log' -PassThru; [IO.File]::WriteAllText('.server.pid', $p.Id)"

REM Give the server a moment to bind the port, then probe /health
>nul timeout /t 2 /nobreak
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://%HOST%:%PORT%/health; if ($r.StatusCode -lt 400) { exit 0 } else { exit 1 } } catch { exit 1 }"
if errorlevel 1 (
  echo Server probe failed. Tail of logs/server.err.log:
  powershell -NoProfile -Command "if (Test-Path 'logs/server.err.log') { Get-Content 'logs/server.err.log' -Tail 50 } else { Write-Host 'no server.err.log yet' }"
  echo Falling back to foreground start for diagnostics...
  call start_debug.bat
  goto :eof
)

REM Start worker in background and save PID (hidden window), logs redirected
powershell -NoProfile -Command "$args = @('-m','app.worker'); $p = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList $args -WorkingDirectory '.' -WindowStyle Hidden -RedirectStandardOutput 'logs/worker.out.log' -RedirectStandardError 'logs/worker.err.log' -PassThru; [IO.File]::WriteAllText('.worker.pid', $p.Id)"

echo Restart complete. Server logs: logs\server.out.log / logs\server.err.log. Worker logs: logs\worker.out.log / logs\worker.err.log
endlocal