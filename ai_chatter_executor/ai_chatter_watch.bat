@echo off
setlocal

cd /d "%~dp0"

python "%~dp0ai_chatter_watch.py" --queue "%APPDATA%\Gajim\Plugins\aichatter_bridge\ai_chatter_tasks.jsonl" --interval 5 --max-tasks 1 --max-jobs 1 --timeout-ms 60000

echo.
echo AI Chatter Watch stopped.
pause
