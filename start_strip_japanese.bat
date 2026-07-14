@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python strip_japanese.py
pause