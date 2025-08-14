@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Please run start.bat once to set up the environment.
  exit /b 1
)

powershell -NoProfile -Command "$p = Start-Process -FilePath '.\\.venv\\Scripts\\python.exe' -ArgumentList '-m','app.worker' -PassThru; Set-Content -Path '.worker.pid' -Value $p.Id"
endlocal
