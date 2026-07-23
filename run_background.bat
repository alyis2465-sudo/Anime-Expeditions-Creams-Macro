@echo off
setlocal
cd /d "%~dp0"
py run_background.py
if errorlevel 1 pause
