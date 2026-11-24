@echo off
setlocal enabledelayedexpansion
title ata - transcricao local de reunioes
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8756"

rem --- primeira execucao: cria o .venv e instala tudo -------------------
if not exist "%PY%" (
    echo Preparando ambiente pela primeira vez ^(pode levar alguns minutos^)...
    where python >nul 2>&1 && ( python -m venv .venv ) || ( py -m venv .venv )
    if not exist "%PY%" (
        echo [ERRO] Python nao encontrado no PATH. Instale o Python 3.11+ e tente de novo.
        pause & exit /b 1
    )
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 ( echo [ERRO] Falha ao instalar dependencias. & pause & exit /b 1 )
)

if not exist "config.toml" copy /y config.example.toml config.toml >nul

rem --- abre o navegador quando o servidor responder --------------------
start "" /min powershell -NoProfile -Command ^
  "1..60 | ForEach-Object { try { Invoke-WebRequest -UseBasicParsing http://%HOST%:%PORT%/api/health -TimeoutSec 1 | Out-Null; Start-Process 'http://%HOST%:%PORT%/'; break } catch { Start-Sleep 1 } }"

echo.
echo   ata  ^>  http://%HOST%:%PORT%
echo   Feche esta janela para encerrar.
echo.

"%PY%" -m ata --host %HOST% --port %PORT% --no-browser

if errorlevel 1 pause
endlocal
