@echo off
title UnderCover
rem Starts the UnderCover web UI and opens it in your browser.
rem  - Double-clicked (or from the Desktop shortcut): runs in the background, no window. Quit it from the page
rem    (SafeHouse, "Quit UnderCover"); its output goes to logs\server.log.
rem  - Run from a terminal: stays in that window with its output; Ctrl+C stops it.
rem  - Force either way: UnderCover.bat --console   or   UnderCover.bat --background
rem (in scripts\: the app folder is one up, and everything below runs from there)
cd /d "%~dp0.."
set "UC_MODE="
set "UC_ARGS=%*"
if /i "%~1"=="--console" (set "UC_MODE=console" & set "UC_ARGS=")
if /i "%~1"=="--background" (set "UC_MODE=background" & set "UC_ARGS=")
rem Started by Explorer (double-click or the Desktop shortcut)? Walk up: powershell <- for's cmd <- this cmd <- parent
if not defined UC_MODE for /f "usebackq delims=" %%P in (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "$i=$PID; foreach($n in 1..3){$i=(Get-CimInstance Win32_Process -Filter ('ProcessId='+$i)).ParentProcessId}; (Get-Process -Id $i -EA 0).ProcessName"`) do set "UC_PARENT=%%P"
if not defined UC_MODE if /i "%UC_PARENT%"=="explorer" set "UC_MODE=background"
if not defined UC_MODE set "UC_MODE=console"
if "%UC_MODE%"=="background" (
  if not exist logs mkdir logs
  start "" ".venv\Scripts\pythonw.exe" -m undercover.core.relaunch 0
  exit /b 0
)
".venv\Scripts\python.exe" -m undercover.server %UC_ARGS%
if errorlevel 1 pause
