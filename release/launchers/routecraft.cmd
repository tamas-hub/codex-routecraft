@echo off
setlocal
set "PYTHONUTF8=1"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 "%~dp0app\routecraft.py" %*
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel% equ 0 (
  python "%~dp0app\routecraft.py" %*
  exit /b %errorlevel%
)
echo Python 3.11 or later was not found on PATH. 1>&2
exit /b 127
