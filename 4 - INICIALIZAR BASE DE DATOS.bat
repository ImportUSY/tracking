@echo off
rem Vacia remesas, facturas, conductores, empresas e historial.
rem Deja solo feriados y usuarios Supervisor, Cobranzas y Consultor.
rem Ver "LEEME_Inicializar_BD.txt".
call "%~dp0Inicializar_BD_Tracking.bat" %*
