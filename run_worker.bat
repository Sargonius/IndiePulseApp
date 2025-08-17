@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Please run start.bat once to set up the environment.
  exit /b 1
)

if not exist logs mkdir logs
powershell -NoProfile -Command "$p = Start-Process -FilePath '.\\.venv\\Scripts\\python.exe' -ArgumentList '-m','app.worker' -RedirectStandardOutput 'logs\\worker.out.log' -RedirectStandardError 'logs\\worker.err.log' -PassThru; Set-Content -Path '.worker.pid' -Value $p.Id"
echo Worker started. Logs: logs\worker.out.log / logs\worker.err.log
endlocal
