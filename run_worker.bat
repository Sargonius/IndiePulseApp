@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Please run start.bat once to set up the environment.
  exit /b 1
)

start "IndiePulse Worker" cmd /k ".venv\Scripts\python.exe -m app.worker"
echo Worker started in a visible window titled 'IndiePulse Worker'.
endlocal
