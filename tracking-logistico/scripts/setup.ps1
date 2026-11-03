# Instalación local en Windows (PowerShell).
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $python = "py -3" } else { $python = "python" }

Write-Host "==> Creando entorno virtual .venv"
Invoke-Expression "$python -m venv .venv"
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pip install -e .

if (-not (Test-Path .env)) {
    Write-Host "==> Creando .env de desarrollo"
    $secret = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
    (Get-Content .env.example) `
        -replace '^APP_ENV=.*', 'APP_ENV=development' `
        -replace '^LOG_JSON=.*', 'LOG_JSON=false' `
        -replace '^SECRET_KEY=.*', "SECRET_KEY=$secret" |
        Set-Content .env -Encoding UTF8
    Write-Host "   Edite .env y defina BOOTSTRAP_ADMIN_PASSWORD (o use: tracking crear-supervisor)"
}

Write-Host "==> Migrando base de datos"
& .\.venv\Scripts\tracking.exe migrate
Write-Host ""
Write-Host "Listo. Siguientes pasos:"
Write-Host "  .\.venv\Scripts\tracking.exe crear-supervisor"
Write-Host "  powershell -File scripts\run_dev.ps1"
