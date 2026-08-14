@echo off
set "PROJ=%~dp0"
if not exist "%PROJ%venv\Scripts\python.exe" set "PROJ=D:\whisper_asmr\"
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%"
rem HuggingFace mirror for China network
set HF_ENDPOINT=https://hf-mirror.com
"%PROJ%venv\Scripts\python.exe" "%PROJ%run_all.py" pause
