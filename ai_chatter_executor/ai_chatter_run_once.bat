@echo off
setlocal

cd /d "%~dp0"

python "%~dp0ai_chatter_run_once.py" --queue "%APPDATA%\Gajim\Plugins\aichatter_bridge\ai_chatter_tasks.jsonl" --max-tasks 1 --max-jobs 1 --timeout-ms 60000

echo.
echo If the run was successful, go to Gajim and type:
echo   .deliver
echo   .results
echo.
pause
