@echo off
setlocal
set "AI_CHATTER_HOME=%~dp0.."
if exist "C:\Python314\python.exe" (
  "C:\Python314\python.exe" "%~dp0ai_chatter_native_host.py"
) else (
  python "%~dp0ai_chatter_native_host.py"
)
