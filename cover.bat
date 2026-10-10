@echo off
rem Usage: cover song.mp3 --voice plankton --pitch 4
"%~dp0.venv\Scripts\python.exe" -m undercover.cli %*
