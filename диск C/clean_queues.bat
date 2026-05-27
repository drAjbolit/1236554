@echo off
setlocal

set "PLUGIN_DIR=%APPDATA%\Gajim\Plugins\aichatter_bridge"

del "%PLUGIN_DIR%\ai_chatter_chrome_jobs.jsonl" /q 2>nul
del "%PLUGIN_DIR%\ai_chatter_chrome_results.jsonl" /q 2>nul
del "%PLUGIN_DIR%\ai_chatter_chrome_worker_state.json" /q 2>nul
del "%PLUGIN_DIR%\ai_chatter_chrome_intake_state.json" /q 2>nul
del "%PLUGIN_DIR%\ai_chatter_run_once.lock" /q 2>nul

echo Cleaned Chrome jobs/results/state/intake/lock in:
echo %PLUGIN_DIR%
pause
