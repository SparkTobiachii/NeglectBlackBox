@echo off
rem Neglect Black Box: starts VRChat through Steam WITH debug logging (the avatar ID test needs it; the options only work at the start of the game).
rem Double-click it, or use the Start menu entry "VRChat (Black Box)". Nothing is installed or changed; Steam's own files are not touched.
setlocal
set "ARGS=--enable-sdk-log-levels --log-debug-levels=API"

tasklist /fi "imagename eq VRChat.exe" 2>nul | find /i "VRChat.exe" >nul
if not errorlevel 1 (
  echo VRChat is already running. Close it completely first, the options only work when the game starts.
  pause
  exit /b 1
)

set "STEAM="
for /f "tokens=2,*" %%a in ('reg query "HKCU\Software\Valve\Steam" /v SteamExe 2^>nul') do set "STEAM=%%b"
if not defined STEAM if exist "%ProgramFiles(x86)%\Steam\steam.exe" set "STEAM=%ProgramFiles(x86)%\Steam\steam.exe"
if not defined STEAM (
  echo Steam was not found. Put this in Steam instead: Library, VRChat, Properties, Launch options:
  echo %ARGS%
  pause
  exit /b 1
)
set "STEAM=%STEAM:/=\%"

start "" "%STEAM%" -applaunch 438100 %ARGS%
