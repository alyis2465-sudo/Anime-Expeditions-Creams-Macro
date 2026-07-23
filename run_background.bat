@echo off
setlocal
cd /d "%~dp0"
python run_background.py
if errorlevel 1 pause
