@echo off
setlocal

REM AI Chatter Chrome CDP launcher
REM Starts Chrome with remote debugging port 9222 and a separate AI Chatter profile.

set CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe
set CHROME_EXE_X86=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
set CDP_PORT=9222
set PROFILE_DIR=G:\AI_chatter\chrome-cdp-profile
set START_URL=https://chatgpt.com/

echo.
echo === AI Chatter Chrome CDP Launcher ===
echo.

REM Check if CDP is already running.
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:%CDP_PORT%/json/version | Out-Null; exit 0 } catch { exit 1 }"
if %ERRORLEVEL%==0 (
    echo Chrome CDP already running on port %CDP_PORT%.
    echo Opening %START_URL% in existing CDP Chrome...
    start "" "http://127.0.0.1:%CDP_PORT%"
    goto :done
)

if exist "%CHROME_EXE%" (
    set CHROME_PATH=%CHROME_EXE%
) else (
    if exist "%CHROME_EXE_X86%" (
        set CHROME_PATH=%CHROME_EXE_X86%
    ) else (
        echo ERROR: Chrome executable not found.
        echo Tried:
        echo   %CHROME_EXE%
        echo   %CHROME_EXE_X86%
        pause
        exit /b 1
    )
)

if not exist "%PROFILE_DIR%" (
    mkdir "%PROFILE_DIR%"
)

echo Chrome:  %CHROME_PATH%
echo Profile: %PROFILE_DIR%
echo Port:    %CDP_PORT%
echo URL:     %START_URL%
echo.

start "AI Chatter Chrome CDP" "%CHROME_PATH%" --remote-debugging-port=%CDP_PORT% --user-data-dir="%PROFILE_DIR%" "%START_URL%"

echo Waiting for CDP...
timeout /t 3 /nobreak >nul

powershell -NoProfile -Command "try { $v = Invoke-RestMethod http://127.0.0.1:%CDP_PORT%/json/version; Write-Host 'CDP OK:' $v.webSocketDebuggerUrl; exit 0 } catch { Write-Host 'CDP not ready yet. Try checking manually:'; Write-Host 'Invoke-RestMethod http://127.0.0.1:%CDP_PORT%/json/version'; exit 1 }"

:done
echo.
echo If this is a new Chrome profile, open:
echo   chrome://extensions
echo and load unpacked:
echo   G:\AI_chatter\ai_chatter_chrome_add_on
echo.
pause
endlocal
