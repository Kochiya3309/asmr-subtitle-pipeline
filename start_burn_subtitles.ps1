$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& (Join-Path $projectRoot 'venv\Scripts\python.exe') (Join-Path $projectRoot 'burn_subtitles.py')
