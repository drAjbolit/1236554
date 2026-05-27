@echo off
setlocal
if exist "C:\Python314\python.exe" (
  "C:\Python314\python.exe" "%~dp0ai_chatter_bus_job_mock.py" %*
) else (
  python "%~dp0ai_chatter_bus_job_mock.py" %*
)
