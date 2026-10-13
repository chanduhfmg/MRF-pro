# Local dev launcher (Windows PowerShell).
# Sets up (venv, deps, .env, DB, node_modules) in THIS window, then opens
# three terminal windows: API :8000, worker, Vite :5173.
#
#   ./scripts/dev.ps1
#   ./scripts/dev.ps1 -SetupOnly     # just prepare, don't launch

#   ./scripts/dev.ps1 -Stub           # simulate extraction (no real MRF needed)
#
# Default is the REAL engine, so an NPI/TIN search downloads and parses actual
# payer files. -Stub keeps the old fake-progress behaviour for UI work.

param([switch]$SetupOnly, [switch]$Stub)

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
  $mode = if ($Stub) { "stub" } else { "real" }
  Step "Writing backend/.env (SQLite + $mode engine)"
  (Get-Content (Join-Path $backend ".env.example")) `
    -replace '^EXTRACTION_MODE=.*', "EXTRACTION_MODE=$mode" |
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
Write-Host "  UI      http://localhost:5173"        -ForegroundColor Green
Write-Host "  Search  http://localhost:5173/search" -ForegroundColor Green
Write-Host "  API     http://localhost:8000/docs"   -ForegroundColor Green
Write-Host ""
Write-Host "Close the three spawned windows (or Ctrl+C in each) to stop." -ForegroundColor DarkGray
