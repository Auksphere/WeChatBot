$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'local.config.json') -Raw | ConvertFrom-Json
$pythonw = Join-Path $config.deployment_root '.venv\Scripts\pythonw.exe'
Start-Process -FilePath $pythonw -ArgumentList @(('"' + (Join-Path $projectRoot 'launch.py') + '"'), '--root', ('"' + $config.deployment_root + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden
