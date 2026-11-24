@echo off
rem Reinicia o ata: mata a instancia atual (se houver), atualiza dependencias
rem (pega libs novas do requirements.txt, ex.: quando o codigo muda) e sobe de novo.
setlocal enabledelayedexpansion
title ata - reiniciar servidor
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8756"

if not exist "%PY%" (
    echo [ERRO] .venv nao encontrado. Rode ata.bat uma vez primeiro para criar o ambiente.
    pause & exit /b 1
)

echo Encerrando instancia anterior do ata (porta %PORT%), se houver...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"%HOST%:%PORT% .*LISTENING"') do (
    echo   encerrando PID %%p
    taskkill /F /PID %%p >nul 2>&1
)

echo Atualizando dependencias...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 ( echo [ERRO] Falha ao instalar dependencias. & pause & exit /b 1 )

if not exist "config.toml" copy /y config.example.toml config.toml >nul

rem --- abre o navegador quando o servidor responder --------------------
start "" /min powershell -NoProfile -Command ^
  "1..60 | ForEach-Object { try { Invoke-WebRequest -UseBasicParsing http://%HOST%:%PORT%/api/health -TimeoutSec 1 | Out-Null; Start-Process 'http://%HOST%:%PORT%/'; break } catch { Start-Sleep 1 } }"

echo.
echo   ata  ^>  http://%HOST%:%PORT%   (reiniciado com o codigo atual)
echo   Feche esta janela para encerrar.
echo.

"%PY%" -m ata --host %HOST% --port %PORT% --no-browser

if errorlevel 1 pause
endlocal
