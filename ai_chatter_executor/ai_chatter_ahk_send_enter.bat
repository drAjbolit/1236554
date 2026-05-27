@echo off
setlocal

set AHK_EXE=
if exist "%ProgramFiles%\AutoHotkey\v2\AutoHotkey64.exe" set AHK_EXE=%ProgramFiles%\AutoHotkey\v2\AutoHotkey64.exe
if exist "%ProgramFiles%\AutoHotkey\AutoHotkey.exe" set AHK_EXE=%ProgramFiles%\AutoHotkey\AutoHotkey.exe
if exist "%ProgramFiles(x86)%\AutoHotkey\AutoHotkey.exe" set AHK_EXE=%ProgramFiles(x86)%\AutoHotkey\AutoHotkey.exe

if "%AHK_EXE%"=="" (
  echo ERROR: AutoHotkey executable not found.
  echo Install AutoHotkey v2 or add it to PATH.
  exit /b 1
)

"%AHK_EXE%" "%~dp0ai_chatter_ahk_send.ahk" enter
exit /b %ERRORLEVEL%
