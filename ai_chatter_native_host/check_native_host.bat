@echo off
setlocal enabledelayedexpansion
set "KEY=HKCU\Software\Google\Chrome\NativeMessagingHosts\ai_chatter.native_host"
echo Registry key:
reg query "%KEY%"
echo.
for /f "tokens=2,*" %%A in ('reg query "%KEY%" /ve 2^>nul ^| find "REG_SZ"') do set "MANIFEST=%%B"
echo Manifest path: !MANIFEST!
echo.
echo Manifest content:
if defined MANIFEST (
  type "!MANIFEST!"
) else (
  echo ERROR: native host registry key not found
)
