# ata v2 — sobe o app local. Cria o .venv na primeira vez.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Criando .venv..." -ForegroundColor Cyan
    $base = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $base) { $base = (Get-Command py -ErrorAction Stop) }
    & $base.Source -m venv .venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt
}

if (-not (Test-Path "config.toml")) {
    Copy-Item "config.example.toml" "config.toml"
    Write-Host "config.toml criado a partir do exemplo — ajuste se quiser." -ForegroundColor Yellow
}

& $py -m ata @args
