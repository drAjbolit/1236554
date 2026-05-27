@echo off
setlocal

set "PYTHON_EXE=C:\Users\789\AppData\Local\Programs\Python\Python314\python.exe"
set "EXECUTOR_DIR=C:\AI_chatter\ai_chatter_executor"
set "QUEUE=%APPDATA%\Gajim\Plugins\aichatter_bridge\ai_chatter_tasks.jsonl"

cd /d "%EXECUTOR_DIR%"

"%PYTHON_EXE%" "%EXECUTOR_DIR%\ai_chatter_run_once.py" --queue "%QUEUE%" --max-tasks 1 --max-jobs 1 --timeout-ms 60000

echo.
echo If the run was successful, go to Gajim and type:
echo   .deliver
echo   .results
echo.
pause
