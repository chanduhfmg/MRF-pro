# Deploying to the server (172.22.6.98)

No Docker. Three long-running pieces: **MySQL**, the **FastAPI API**, and the
**worker**. The frontend is a static build served by **nginx**, which also
reverse-proxies `/api` to the API (so the UI and API share an origin and the
build needs no API URL baked in).

```
            ┌─────────── nginx :80 ───────────┐
browser ───▶│  /            → frontend/dist    │
            │  /api/*       → 127.0.0.1:8000   │──▶ uvicorn (app.main:app)
            └─────────────────────────────────┘         │
                                                        ▼
                     python -m app.worker  ◀── polls ── MySQL  (extraction_jobs)
                          │
                          ▼
                 /opt/mrf-dashboard/data/{raw,output}
```

## 0. One-time: users, code, MySQL

```bash
sudo useradd -r -m -d /opt/mrf-dashboard mrf
sudo mkdir -p /opt/mrf-dashboard && sudo chown mrf:mrf /opt/mrf-dashboard
sudo -u mrf git clone <repo-url> /opt/mrf-dashboard      # or rsync the tree

# MySQL (already installed per your setup)
sudo mysql < /opt/mrf-dashboard/backend/schema.sql
sudo mysql -e "CREATE USER 'mrf'@'localhost' IDENTIFIED BY 'CHANGE-ME';
               GRANT ALL PRIVILEGES ON mrf_dashboard.* TO 'mrf'@'localhost';
               FLUSH PRIVILEGES;"
```

## 1. Backend

```bash
sudo -u mrf bash -lc '
  cd /opt/mrf-dashboard/backend
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  cp .env.example .env
'
```

Edit `/opt/mrf-dashboard/backend/.env`:

```ini
DATABASE_URL=mysql+pymysql://mrf:CHANGE-ME@127.0.0.1:3306/mrf_dashboard
DATA_ROOT=/opt/mrf-dashboard/data
CORS_ORIGINS=http://172.22.6.98
EXTRACTION_MODE=real
NPI_LOOKUP_THREADS=8
CSV_ROW_THRESHOLD=500000
```

Create tables (idempotent) and install the services:

```bash
sudo -u mrf /opt/mrf-dashboard/backend/.venv/bin/python -m scripts.init_db   # run from backend/
sudo cp /opt/mrf-dashboard/deploy/mrf-api.service    /etc/systemd/system/
sudo cp /opt/mrf-dashboard/deploy/mrf-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mrf-api mrf-worker
```

Run extra workers if extraction throughput matters — they coordinate through
the DB (`SELECT ... FOR UPDATE SKIP LOCKED`), no config change needed:

```bash
sudo cp /etc/systemd/system/mrf-worker.service /etc/systemd/system/mrf-worker2.service
sudo systemctl enable --now mrf-worker2
```

## 2. Frontend

```bash
sudo -u mrf bash -lc '
  cd /opt/mrf-dashboard/frontend
  npm ci
  npm run build            # -> frontend/dist  (no .env needed: same-origin via nginx)
'
sudo cp /opt/mrf-dashboard/deploy/nginx-mrf.conf /etc/nginx/sites-available/mrf
sudo ln -sf /etc/nginx/sites-available/mrf /etc/nginx/sites-enabled/mrf
sudo nginx -t && sudo systemctl reload nginx
```

Open **http://172.22.6.98/**.

### If you can't use nginx / want the API to serve the UI

Set in `backend/.env`:

```ini
FRONTEND_DIST=/opt/mrf-dashboard/frontend/dist
```

then `sudo systemctl restart mrf-api` and hit **http://172.22.6.98:8000/**.
(Uploads of many-GB files are happier behind nginx, though.)

### If UI and API end up on different origins/ports

Build the frontend with the API origin baked in:

```bash
echo 'VITE_API_BASE_URL=http://172.22.6.98:8000' > frontend/.env
npm run build
```

## 3. Updating a deployed instance

```bash
cd /opt/mrf-dashboard && sudo -u mrf git pull
sudo -u mrf backend/.venv/bin/pip install -r backend/requirements.txt
sudo -u mrf bash -lc 'cd frontend && npm ci && npm run build'
sudo -u mrf backend/.venv/bin/python -m scripts.init_db     # picks up new columns
sudo systemctl restart mrf-api mrf-worker
```

## Operations

| task | command |
|---|---|
| logs | `journalctl -u mrf-api -f` · `journalctl -u mrf-worker -f` |
| health | `curl http://127.0.0.1:8000/api/health` |
| smoke test (stub) | `cd backend && EXTRACTION_MODE=stub .venv/bin/python -m pytest -q` |
| disk usage | `du -sh /opt/mrf-dashboard/data/output` |
| stuck job? | worker auto-requeues jobs with no heartbeat for `WORKER_STUCK_TIMEOUT` (900s), then fails after `WORKER_MAX_RETRIES` |

Output archives live at `DATA_ROOT/output/{job_id}/{job_id}.zip`; raw uploads
at `DATA_ROOT/raw/{company}/`. Both are safe to prune once downloaded — the
job row keeps the metadata and the UI will show the archive as missing.
