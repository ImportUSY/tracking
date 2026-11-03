@echo off
title Tracking Logistico - publicar en Vercel
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\deploy_vercel.ps1"
echo.
pause
