@echo off
setlocal

set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "AI_CHATTER_HOME=C:\AI_chatter"
set "CDP_PROFILE=%AI_CHATTER_HOME%\chrome-cdp-profile"

if not exist "%CHROME_EXE%" (
  echo Chrome not found:
  echo   "%CHROME_EXE%"
  pause
  exit /b 1
)

start "" "%CHROME_EXE%" --remote-debugging-port=9222 --user-data-dir="%CDP_PROFILE%" --new-window https://chatgpt.com/

echo Chrome CDP started on http://127.0.0.1:9222
echo Profile: %CDP_PROFILE%
pause
