@echo off
set "PROJ=%~dp0"
if not exist "%PROJ%venv\Scripts\python.exe" set "PROJ=D:\whisper_asmr\"
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%"
"%PROJ%venv\Scripts\python.exe" "%PROJ%strip_japanese.py" pause
