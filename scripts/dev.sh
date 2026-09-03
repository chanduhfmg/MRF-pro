#!/usr/bin/env bash
# Local dev launcher (Linux/macOS/Git-Bash). Runs API + worker + Vite together.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

py="$root/backend/.venv/bin/python"
[ -x "$py" ] || py="$root/backend/.venv/Scripts/python.exe"   # git-bash on Windows

if [ ! -x "$py" ]; then
  echo "Creating backend venv..."
  python3 -m venv "$root/backend/.venv"
  py="$root/backend/.venv/bin/python"; [ -x "$py" ] || py="$root/backend/.venv/Scripts/python.exe"
  "$py" -m pip install --upgrade pip
  "$py" -m pip install -r "$root/backend/requirements.txt"
fi

if [ ! -f "$root/backend/.env" ]; then
  cp "$root/backend/.env.example" "$root/backend/.env"
  # local dev default: stub engine so any tiny .json works end-to-end
  sed -i.bak 's/^EXTRACTION_MODE=.*/EXTRACTION_MODE=stub/' "$root/backend/.env" && rm -f "$root/backend/.env.bak"
  echo "Created backend/.env (SQLite + stub engine)."
fi
( cd "$root/backend" && "$py" -m scripts.init_db )
[ -d "$root/frontend/node_modules" ] || ( cd "$root/frontend" && npm install )

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

( cd "$root/backend" && "$py" -m uvicorn app.main:app --reload --port 8000 ) & pids+=($!)
( cd "$root/backend" && "$py" -m app.worker ) & pids+=($!)
( cd "$root/frontend" && npm run dev ) & pids+=($!)

echo -e "\n  UI   http://localhost:5173\n  API  http://localhost:8000/docs\n\nCtrl+C to stop all.\n"
wait
