@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python rename_suffix.py _cn_only
pause
