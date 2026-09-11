$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$env:PYTHONPATH = "$projectRoot\\"
$env:HF_ENDPOINT = 'https://hf-mirror.com'

Set-Location -LiteralPath $projectRoot
& (Join-Path $projectRoot 'venv\Scripts\python.exe') (Join-Path $projectRoot 'run_all.py')
