@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Subir version a GitHub - Tracking Logistico
set "REPO=https://github.com/ImportUSY/tracking.git"

echo ============================================================
echo   Respaldo en GitHub - Tracking Logistico
echo   Repositorio: %REPO%
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git no esta instalado. Descarguelo de https://git-scm.com y vuelva a intentar.
    goto :fin_error
)

rem La carpeta puede pertenecer a otro usuario de Windows: git exige marcarla como segura
set "CARPETA=%CD:\=/%"
git config --global --get-all safe.directory 2>nul | findstr /x /c:"%CARPETA%" >nul
if errorlevel 1 git config --global --add safe.directory "%CARPETA%"

if not exist ".git" (
    echo Primera vez: creando el repositorio local...
    git init -b main || goto :fin_error
)
git remote get-url origin >nul 2>nul
if errorlevel 1 git remote add origin "%REPO%"

rem Nombre con que se firman los cambios (solo si falta)
git config user.name >nul 2>nul
if errorlevel 1 (
    set /p "GNOMBRE=Su nombre para firmar los cambios: "
    git config user.name "!GNOMBRE!"
)
git config user.email >nul 2>nul
if errorlevel 1 (
    set /p "GCORREO=Su correo de GitHub: "
    git config user.email "!GCORREO!"
)

rem Version actual del programa (tracking-logistico\src\tracking\__init__.py)
set "VER="
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"__version__" "tracking-logistico\src\tracking\__init__.py"') do set "VER=%%v"
set "VER=!VER:"=!"
set "VER=!VER: =!"
if "!VER!"=="" set "VER=sin-version"
echo Version del programa: !VER!
echo.

git add -A || goto :fin_error

rem Freno de seguridad: claves, llave de firma del APK y bases de datos nunca se suben
git diff --cached --name-only | findstr /i /r "keystore signing-key LLAVE_DE_FIRMA \.env$ \.env\.local tracking\.db \.zip$" >nul
if not errorlevel 1 (
    echo [DETENIDO] Hay archivos con claves o la llave de firma preparados para subir:
    git diff --cached --name-only | findstr /i /r "keystore signing-key LLAVE_DE_FIRMA \.env$ \.env\.local tracking\.db \.zip$"
    echo Revise el archivo .gitignore. No se subio nada.
    git reset -q
    goto :fin_error
)

git diff --cached --quiet
if errorlevel 1 (
    echo Cambios que se van a subir:
    git status --short
    echo.
    set "MSG="
    set /p "MSG=Describa el cambio (Enter = Version !VER!): "
    if "!MSG!"=="" set "MSG=Version !VER!"
    git commit -q -m "!MSG!" || goto :fin_error
    echo Cambios guardados: !MSG!
) else (
    echo No hay cambios nuevos en los archivos.
)

rem Marca de version (v1.26.0, etc.) la primera vez que se sube cada version
git rev-parse -q --verify "refs/tags/v!VER!" >nul
if errorlevel 1 if not "!VER!"=="sin-version" git tag -a "v!VER!" -m "Version !VER!"

echo.
echo Subiendo a GitHub (la primera vez se abre el navegador para iniciar sesion)...
git push -u origin main --follow-tags
if errorlevel 1 (
    echo.
    echo GitHub tiene cambios que esta PC no tiene. Trayendolos y reintentando...
    git pull --rebase --autostash origin main || goto :fin_error
    git push -u origin main --follow-tags || goto :fin_error
)

echo.
echo ============================================================
echo   LISTO: version !VER! respaldada en GitHub.
echo   Vea el historial en https://github.com/ImportUSY/tracking
echo ============================================================
pause
exit /b 0

:fin_error
echo.
echo No se pudo completar el respaldo. Revise el mensaje de arriba.
pause
exit /b 1
