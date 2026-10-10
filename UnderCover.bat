@echo off
title UnderCover
rem Starts the UnderCover web UI and opens it in your browser. Keep this window open while you use it.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m undercover.server %*
if errorlevel 1 pause
