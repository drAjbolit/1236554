@echo off
setlocal EnableExtensions
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install_native_host.py"
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0install_native_host.py"
  exit /b %errorlevel%
)
echo ERROR: Python not found in PATH. Try: C:\Python314\python.exe "%~dp0install_native_host.py"
exit /b 1
