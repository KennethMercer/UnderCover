@echo off
rem Usage: cover song.mp3 --voice plankton --pitch 4
rem (in scripts\: the app folder is one up; your file names stay relative to where you run it)
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
"%~dp0..\.venv\Scripts\python.exe" -m undercover.cli %*
