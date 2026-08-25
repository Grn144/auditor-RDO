@echo off
chcp 65001 >nul
title Auditor RDO - Diario de Obra
cd /d "%~dp0"

echo ============================================
echo   Auditor de RDO - Diario de Obra
echo ============================================
echo.
echo   Iniciando o aplicativo... o navegador vai
echo   abrir sozinho em instantes.
echo.
echo   NAO feche esta janela enquanto usar o app.
echo   (feche-a para encerrar o aplicativo)
echo.

python app.py

echo.
echo   O aplicativo foi encerrado.
pause
