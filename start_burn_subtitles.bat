@echo off
setlocal
set "PROJ=%~dp0"
if not exist "%PROJ%venv\Scripts\python.exe" set "PROJ=D:\whisper_asmr\"
set "WT=%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"

if exist "%WT%" (
    start "" "%WT%" new-tab --title "Burn Subtitles" -- powershell.exe -NoExit -ExecutionPolicy Bypass -File "%PROJ%start_burn_subtitles.ps1"
    exit /b
)

echo Windows Terminal (wt.exe) is unavailable. Running in this CMD window instead.
cd /d "%PROJ%"
"%PROJ%venv\Scripts\python.exe" "%PROJ%burn_subtitles.py"
pause
