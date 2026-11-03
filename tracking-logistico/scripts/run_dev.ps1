# Arranca el servidor de desarrollo en http://localhost:8000 (Windows).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\tracking.exe serve --host 0.0.0.0 --port 8000 --reload
