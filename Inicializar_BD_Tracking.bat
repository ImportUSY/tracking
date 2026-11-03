@echo off
REM ============================================================================
REM  Tracking Logistico - Inicializacion y limpieza de la base de datos
REM
REM  VACIA:    remesas, facturas (historial, fotos, capturas, cargas de Excel),
REM            conductores (y sus usuarios), camiones, auditoria, bitacora y
REM            consultas publicas.
REM  CONSERVA: empresas, vendedores, feriados y usuarios de oficina
REM            (supervisor, transportista, cobranzas y consultor).
REM
REM  Antes de borrar guarda un respaldo completo en  respaldos_bd\  (junto a
REM  este archivo). Todo ocurre en una sola transaccion y se valida antes de
REM  confirmar: si algo no cuadra, se deshace y no se borra nada.
REM
REM  USO
REM    Doble clic ....................................... menu interactivo
REM    Inicializar_BD_Tracking.bat local --si ........... local, desatendido
REM    Inicializar_BD_Tracking.bat produccion --si ...... Neon/Vercel, desatendido
REM    Inicializar_BD_Tracking.bat local --simular ...... prueba sin cambiar nada
REM
REM  Codigos de salida: 0 = terminado, 1 = cancelado o error,
REM                     2 = la validacion fallo (no se cambio nada)
REM ============================================================================
setlocal EnableExtensions
chcp 65001 >nul
set "RAIZ=%~dp0"
set "PROYECTO=%RAIZ%tracking-logistico"
set "RESPALDOS=%RAIZ%respaldos_bd"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONPATH=%PROYECTO%\src"

if not exist "%PROYECTO%\src\tracking\inicializar_bd.py" (
  echo No se encontro el proyecto en "%PROYECTO%".
  echo Este archivo debe estar en la carpeta que contiene "tracking-logistico".
  if "%~1"=="" pause
  exit /b 1
)

REM --- Python: el entorno virtual del proyecto; si no existe, el Python instalado
set "PY="
if exist "%PROYECTO%\.venv\Scripts\python.exe" set "PY="%PROYECTO%\.venv\Scripts\python.exe""
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo No se encontro Python. Ejecute primero scripts\probar_local.bat para crear el entorno.
  if "%~1"=="" pause
  exit /b 1
)

cd /d "%PROYECTO%"

REM --- Modo desatendido: el primer parametro es el destino (local o produccion)
if "%~1"=="" goto menu
if /i "%~1"=="local" goto desatendido
if /i "%~1"=="produccion" goto desatendido
echo Destino invalido: %~1   Use: local  o  produccion
exit /b 1

:desatendido
%PY% -m tracking.inicializar_bd --destino %~1 --proyecto "%PROYECTO%" --respaldos "%RESPALDOS%" %2 %3 %4
exit /b %ERRORLEVEL%

:menu
cls
echo ==============================================================
echo   TRACKING LOGISTICO - INICIALIZAR BASE DE DATOS
echo ==============================================================
echo   Se VACIAN: remesas, facturas, conductores, camiones e historial
echo   Se CONSERVAN: empresas, vendedores, feriados y usuarios de oficina
echo.
echo   1. Simular en LOCAL        (muestra lo que haria, no cambia nada)
echo   2. Inicializar LOCAL       (data\tracking.db)
echo   3. Simular en PRODUCCION   (no cambia nada)
echo   4. Inicializar PRODUCCION  (base Neon que usa Vercel)
echo   0. Salir
echo.
echo   Para la base LOCAL cierre antes la ventana de probar_local.bat.
echo.
set "OP="
set "ARGS="
set /p "OP=Elija una opcion: "
if "%OP%"=="1" set "ARGS=--destino local --simular"
if "%OP%"=="2" set "ARGS=--destino local"
if "%OP%"=="3" set "ARGS=--destino produccion --simular"
if "%OP%"=="4" set "ARGS=--destino produccion"
if "%OP%"=="0" exit /b 0
if not defined ARGS goto menu

echo.
%PY% -m tracking.inicializar_bd %ARGS% --proyecto "%PROYECTO%" --respaldos "%RESPALDOS%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Proceso terminado correctamente.) else (echo El proceso termino con codigo %RC%. Revise el registro en respaldos_bd.)
pause
exit /b %RC%
