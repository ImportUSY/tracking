@echo off
setlocal
title Tracking Logistico - en tu PC
cd /d "%~dp0.."

rem --- 1. Python ---------------------------------------------------------
set "HAYPY="
py -3 --version >nul 2>&1 && set "HAYPY=1"
if not defined HAYPY python --version >nul 2>&1 && set "HAYPY=1"
if not defined HAYPY (
  echo.
  echo  Falta Python 3.10 o superior.
  echo  Instalelo con este comando en PowerShell:   winget install Python.Python.3.12
  echo  o desde https://www.python.org/downloads/   ^(marque "Add python.exe to PATH"^)
  echo  Luego vuelva a abrir este archivo.
  echo.
  pause
  exit /b 1
)

rem --- 2. Instalacion (solo la primera vez) ------------------------------
if not exist ".venv\Scripts\tracking.exe" (
  echo.
  echo  == Primera vez: instalando el programa. Tarda de 1 a 3 minutos ==
  powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
  if errorlevel 1 goto fallo
)

rem --- 3. Usuario supervisor (solo la primera vez) ------------------------
if not exist "data\supervisor.ok" (
  echo.
  echo  == Cree la clave del usuario "supervisor" ==
  echo     Minimo 6 caracteres. Al escribirla no se ve nada: es normal.
  ".venv\Scripts\tracking.exe" crear-supervisor
  if errorlevel 1 goto fallo
  if not exist data mkdir data
  echo ok> "data\supervisor.ok"
)

rem --- 4. Cerrar una copia anterior que siga abierta ------------------------
rem Si quedo una ventana vieja del programa ocupando el puerto 8000, la nueva no podria arrancar
rem y el navegador seguiria hablando con la version anterior.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
  for /f "tokens=1" %%n in ('tasklist /FI "PID eq %%p" /NH') do (
    if /I "%%n"=="python.exe" (echo  Cerrando una copia anterior del programa ^(PID %%p^)... & taskkill /PID %%p /F >nul 2>&1)
    if /I "%%n"=="tracking.exe" (echo  Cerrando una copia anterior del programa ^(PID %%p^)... & taskkill /PID %%p /F >nul 2>&1)
  )
)

rem --- 5. Arrancar ----------------------------------------------------------
echo.
echo  ============================================================
echo   PROGRAMA EN MARCHA. Deje esta ventana abierta mientras lo usa.
echo.
echo   Panel (Transporte, Cobranzas):  http://localhost:8000
echo   App del conductor:              http://localhost:8000/conductor
echo   Consulta de vendedores:         http://localhost:8000/tracking
echo.
echo   Usuario: supervisor    Clave: la que creo la primera vez
echo   Para detenerlo: cierre esta ventana.
echo  ============================================================
echo.
start "" /min cmd /c "timeout /t 6 /nobreak >nul && start http://localhost:8000"
".venv\Scripts\tracking.exe" serve --host 0.0.0.0 --port 8000
echo.
echo  El programa se detuvo. Si arriba dice que el puerto 8000 esta en uso, cierre las otras
echo  ventanas negras de "Tracking Logistico" y vuelva a abrir este archivo.
pause
goto :eof

:fallo
echo.
echo  Ocurrio un error. Revise los mensajes de arriba o consulte "EMPIEZA AQUI.html".
pause
exit /b 1
