# Publica Tracking Logístico en Vercel desde Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_vercel.ps1
#
# Requisitos: Node.js 18+ (https://nodejs.org o: winget install OpenJS.NodeJS.LTS) y una cuenta en vercel.com.
# El script abre el navegador para que USTED inicie sesión en Vercel; no guarda su contraseña.
param(
    [string]$Proyecto = "tracking-logistico"
)
$ErrorActionPreference = "Continue"  # los comandos de Node escriben avisos en stderr; los errores se revisan con $LASTEXITCODE
Set-Location (Split-Path -Parent $PSScriptRoot)

function Vercel { & npx.cmd --yes vercel@latest @args; if ($LASTEXITCODE -ne 0) { throw "vercel $args falló" } }

function Set-VercelEnv([string]$Nombre, [string]$Valor) {
    # Reemplaza la variable en Producción (si no existía, el borrado falla y se ignora)
    & npx.cmd --yes vercel@latest env rm $Nombre production --yes 2>$null | Out-Null
    $Valor | & npx.cmd --yes vercel@latest env add $Nombre production | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudo guardar la variable $Nombre" }
    Write-Host "   $Nombre configurada"
}

if (-not (Get-Command npx.cmd -ErrorAction SilentlyContinue)) {
    Write-Host "Falta Node.js. Instálelo con:  winget install OpenJS.NodeJS.LTS  (o desde https://nodejs.org) y vuelva a ejecutar." -ForegroundColor Red
    exit 1
}

Write-Host "==> 1/5 Iniciar sesión en Vercel (se abrirá el navegador)" -ForegroundColor Cyan
& npx.cmd --yes vercel@latest whoami 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Vercel login }

Write-Host "==> 2/5 Vincular esta carpeta al proyecto '$Proyecto'" -ForegroundColor Cyan
Vercel link --yes --project $Proyecto

Write-Host "==> 3/5 Base de datos PostgreSQL (Neon, plan gratuito)" -ForegroundColor Cyan
$envs = (& npx.cmd --yes vercel@latest env ls production 2>&1) -join "`n"
if ($envs -notmatch "DATABASE_URL|POSTGRES_URL") {
    Write-Host "   Se instalará la integración Neon. Acepte los términos en el navegador/consola y elija la región más cercana."
    & npx.cmd --yes vercel@latest integration add neon
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   No se pudo hacerlo desde la consola. Hágalo en el panel:" -ForegroundColor Yellow
        Write-Host "   vercel.com > proyecto '$Proyecto' > Storage > Create Database > Neon > Connect (entorno Production)"
    }
    Read-Host "   Cuando la base aparezca conectada al proyecto, presione Enter"
} else {
    Write-Host "   Base de datos ya conectada."
}

Write-Host "==> 4/5 Variables del programa" -ForegroundColor Cyan
$bytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$secret = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
if ($envs -notmatch "SECRET_KEY") { Set-VercelEnv "SECRET_KEY" $secret } else { Write-Host "   SECRET_KEY ya existe (se conserva para no cerrar sesiones)" }
Set-VercelEnv "APP_ENV" "production"
# APK de Android (TWA): el dominio declara qué apps pueden abrirlo a pantalla completa (android\assetlinks.json)
$assetlinks = Join-Path (Get-Location) "android\assetlinks.json"
if (Test-Path $assetlinks) {
    $json = (Get-Content $assetlinks -Raw -Encoding UTF8) -replace '\s*\r?\n\s*', ''  # una sola línea
    Set-VercelEnv "ANDROID_ASSETLINKS_JSON" $json
}
if ($envs -notmatch "BOOTSTRAP_ADMIN_PASSWORD") {
    $p1 = Read-Host "   Clave para el usuario 'supervisor' (mínimo 6 caracteres)" -AsSecureString
    $plano = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    if ($plano.Length -lt 6) { throw "La clave debe tener al menos 6 caracteres" }
    Set-VercelEnv "BOOTSTRAP_ADMIN_PASSWORD" $plano
}

Write-Host "==> 5/5 Publicando en producción" -ForegroundColor Cyan
Vercel deploy --prod --yes

Write-Host ""
Write-Host "Listo. Abra https://$Proyecto.vercel.app  (o la dirección 'Production' que mostró Vercel arriba)" -ForegroundColor Green
Write-Host "  Usuario: supervisor   Clave: la que acaba de escribir"
Write-Host "  Conductores: https://$Proyecto.vercel.app/conductor   Vendedores: https://$Proyecto.vercel.app/tracking"
