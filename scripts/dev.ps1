# Local dev launcher (Windows PowerShell). Runs API + worker + Vite together.
# Usage:  ./scripts/dev.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# --- backend venv ---
$venvPy = Join-Path $root "backend/.venv/Scripts/python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "Creating backend venv..." -ForegroundColor Cyan
  python -m venv (Join-Path $root "backend/.venv")
  & $venvPy -m pip install --upgrade pip
  & $venvPy -m pip install -r (Join-Path $root "backend/requirements.txt")
}

if (-not (Test-Path (Join-Path $root "backend/.env"))) {
  $envPath = Join-Path $root "backend/.env"
  (Get-Content (Join-Path $root "backend/.env.example")) `
    -replace '^EXTRACTION_MODE=.*', 'EXTRACTION_MODE=stub' |
    Set-Content -Encoding utf8 $envPath
  Write-Host "Created backend/.env from example (SQLite + stub engine)." -ForegroundColor Yellow
}

& $venvPy -m scripts.init_db 2>$null; Push-Location (Join-Path $root "backend"); & $venvPy -m scripts.init_db; Pop-Location

# --- frontend deps ---
if (-not (Test-Path (Join-Path $root "frontend/node_modules"))) {
  Push-Location (Join-Path $root "frontend"); npm install; Pop-Location
}

Write-Host "`nStarting API :8000, worker, and Vite :5173 ..." -ForegroundColor Green
$api    = Start-Process -PassThru -NoNewWindow -WorkingDirectory (Join-Path $root "backend") $venvPy "-m","uvicorn","app.main:app","--reload","--port","8000"
$worker = Start-Process -PassThru -NoNewWindow -WorkingDirectory (Join-Path $root "backend") $venvPy "-m","app.worker"
$web    = Start-Process -PassThru -NoNewWindow -WorkingDirectory (Join-Path $root "frontend") "npm" "run","dev"

Write-Host "`n  UI   http://localhost:5173`n  API  http://localhost:8000/docs`n`nCtrl+C to stop all.`n" -ForegroundColor Cyan
try { Wait-Process -Id $api.Id } finally {
  foreach ($p in @($api,$worker,$web)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
}
