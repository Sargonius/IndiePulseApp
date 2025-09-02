@echo off
setlocal

REM Elevate to Administrator if not already (needed to kill some listeners)
net session >nul 2>&1
if %errorlevel% NEQ 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~dpnx0' -WorkingDirectory '%cd%' -Verb RunAs"
  exit /b
)

set HOST=127.0.0.1
set PORT=8000
if exist .env (
  for /f "tokens=1,2 delims==" %%G in (.env) do (
    if /I "%%G"=="APP_HOST" set HOST=%%H
    if /I "%%G"=="APP_PORT" set PORT=%%H
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

REM Kill uvicorn/app and worker processes by command line match (best-effort)
powershell -NoProfile -Command "try { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'app.main:app' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } catch { }"
powershell -NoProfile -Command "try { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app.worker' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } catch { }"

REM Kill any python.exe running from this project's venv explicitly
powershell -NoProfile -Command "try { $py=(Join-Path (Get-Location) '.venv\\Scripts\\python.exe'); Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $py } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } catch { }"

REM Free the server port if still occupied
powershell -NoProfile -Command "$port=%PORT%; $conns=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if($conns){ $pids=($conns | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique); foreach($procId in $pids){ try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } catch {} } }"

REM Netstat-based kill sweep (covers cases where Get-NetTCPConnection is restricted)
for /L %%I in (1,1,3) do (
  for /f "tokens=5" %%P in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    echo Killing PID %%P listening on %PORT%
    taskkill /PID %%P /T /F >nul 2>&1
  )
  >nul timeout /t 1 /nobreak
)

REM Final check
for /f "tokens=5" %%P in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
  echo WARNING: Port %PORT% still has listener PID %%P
)

endlocal