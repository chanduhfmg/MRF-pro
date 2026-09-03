# Local dev launcher (Windows PowerShell).
# Sets up (venv, deps, .env, DB, node_modules) in THIS window, then opens
# three terminal windows: API :8000, worker, Vite :5173.
#
#   ./scripts/dev.ps1
#   ./scripts/dev.ps1 -SetupOnly     # just prepare, don't launch

param([switch]$SetupOnly)

$ErrorActionPreference = "Stop"
$root      = Split-Path -Parent $PSScriptRoot
$backend   = Join-Path $root "backend"
$frontend  = Join-Path $root "frontend"
$venvPy    = Join-Path $backend ".venv\Scripts\python.exe"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# --- 1. backend venv + deps ------------------------------------------------
if (-not (Test-Path $venvPy)) {
  Step "Creating backend venv"
  python -m venv (Join-Path $backend ".venv")
  & $venvPy -m pip install --upgrade pip
  Step "Installing backend requirements"
  & $venvPy -m pip install -r (Join-Path $backend "requirements.txt")
}

# --- 2. backend/.env -----------------------------------------------------
$envFile = Join-Path $backend ".env"
if (-not (Test-Path $envFile)) {
  Step "Writing backend/.env (SQLite + stub engine)"
  (Get-Content (Join-Path $backend ".env.example")) `
    -replace '^EXTRACTION_MODE=.*', 'EXTRACTION_MODE=stub' |
    Set-Content -Encoding utf8 $envFile
}

# --- 3. database (run from backend/ so `scripts.init_db` imports) --------
Step "Initializing database"
Push-Location $backend
try { & $venvPy -m scripts.init_db } finally { Pop-Location }

# --- 4. frontend deps --------------------------------------------------
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
  Step "Installing frontend dependencies (npm install)"
  Push-Location $frontend
  try { npm install } finally { Pop-Location }
}

if ($SetupOnly) { Step "Setup complete."; return }

# --- 5. launch the three processes, each in its own window --------------
Step "Launching API, worker, and Vite in separate windows"
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command","cd '$backend'; & '$venvPy' -m uvicorn app.main:app --reload --port 8000")
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command","cd '$backend'; & '$venvPy' -m app.worker")
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command","cd '$frontend'; npm run dev")

Write-Host ""
Write-Host "  UI    http://localhost:5173"       -ForegroundColor Green
Write-Host "  API   http://localhost:8000/docs"  -ForegroundColor Green
Write-Host ""
Write-Host "Close the three spawned windows (or Ctrl+C in each) to stop." -ForegroundColor DarkGray
