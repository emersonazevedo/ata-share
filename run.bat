@echo off
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo Criando .venv...
    python -m venv .venv || py -m venv .venv || (echo [ERRO] Python nao encontrado no PATH & pause & exit /b 1)
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
)

if not exist "config.toml" copy /y config.example.toml config.toml >nul

"%PY%" -m ata %*
if errorlevel 1 pause
endlocal
