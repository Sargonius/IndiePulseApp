@echo off
rem Same as double-clicking IndiePulse.pyw: tray icon, no console window. Any Python starts it (IndiePulse.pyw makes
rem the .venv itself): the py launcher, or - on a computer without Python - uv's own, with uv from PATH or from ClubKit
rem on this drive (it fetches Python 3.12 once, no install, no admin).
setlocal
where pyw >NUL 2>&1 && (start "" pyw -3.12 "%~dp0IndiePulse.pyw" %* & exit /b 0)

set "UV="
for /f "delims=" %%U in ('where uv 2^>NUL') do if not defined UV set "UV=%%U"
if not defined UV if exist "%~d0\ClubKit\tools\uv\uv.exe" set "UV=%~d0\ClubKit\tools\uv\uv.exe"
if not defined UV (echo Python 3.12 with the py launcher, or uv, is required: https://www.python.org/downloads/ & pause & exit /b 1)

rem the same as app\desktop\deps.uv_env(): ClubKit keeps uv's cache and Pythons on the drive
if exist "%~d0\ClubKit\" (
	if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%~d0\ClubKit\data\uv\cache"
	if not defined UV_PYTHON_INSTALL_DIR set "UV_PYTHON_INSTALL_DIR=%~d0\ClubKit\data\uv\python"
	if not defined UV_PYTHON_PREFERENCE set "UV_PYTHON_PREFERENCE=only-managed"
)
rem --system: never a .venv - the one here may have come from another computer, where its Python lives
set "PY="
for /f "delims=" %%P in ('"%UV%" python find --system 3.12 2^>NUL') do set "PY=%%P"
if not defined PY (
	echo Fetching Python 3.12 with uv...
	"%UV%" python install 3.12 || (pause & exit /b 1)
	for /f "delims=" %%P in ('"%UV%" python find --system 3.12 2^>NUL') do set "PY=%%P"
)
if not defined PY (echo uv could not find Python 3.12 & pause & exit /b 1)
for %%P in ("%PY%") do start "" "%%~dpPpythonw.exe" "%~dp0IndiePulse.pyw" %*
