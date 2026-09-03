# Insurance MRF Extraction Dashboard

Internal tool that automates extracting and packaging **Transparency-in-Coverage
(TiC) machine-readable files (MRFs)** from payers (UHC, Aetna, MVP, Anthem — the
list is extensible). Upload a payer's in-network-rates `.json.gz`, and a
background worker stream-parses it for the target CPT/HCPCS codes, enriches each
row with NPPES provider data, splits the result into CSVs and zips them for
download — with live progress and a KPI dashboard.

```
Download MRF ─▶ Upload (chunked) ─▶ Extract + enrich (worker) ─▶ Track progress ─▶ Package + store ─▶ Download zip
```

## Stack

| Layer | Choice |
|---|---|
| Frontend | React + TypeScript, Vite, Tailwind, Framer Motion, Recharts |
| Backend | Python **FastAPI** + SQLAlchemy |
| DB | **MySQL** on the server; **SQLite** auto-fallback for local dev (no infra) |
| Background jobs | **Redis-free** — a standalone `python -m app.worker` process polls the `extraction_jobs` table. No Celery, no Redis, no Docker. |
| Storage | local disk — `data/raw/{company}/{file}`, `data/output/{job_id}/` |

Extraction never runs in the HTTP request. The worker claims jobs atomically
(`SELECT … FOR UPDATE SKIP LOCKED` on MySQL), heartbeats progress, and
auto-requeues any job whose worker died mid-run — a job is never left silently
stuck in `processing`.

## Repo layout

```
extract_tic_all.py      real extraction logic (ijson 2-pass, NPI==TIN guard) — adapted, still runs standalone
enrich_npi_all.py       real NPPES enrichment (threaded) — adapted, still runs standalone
backend/
  app/
    main.py             FastAPI app + routers + optional SPA hosting
    config.py           all env-driven settings (.env)
    models.py           ExtractionJob ORM (spec DDL + heartbeat/retry cols)
    crud.py             job queries, atomic claim, live KPI aggregates
    routers/jobs.py     chunked upload, list/detail/status, zip download
    routers/kpis.py     /api/kpis, /api/companies, /api/meta
    worker.py           the polling worker (run as its own process)
    pipeline/
      run.py            dispatch: stub | real
      stub.py           fake progress + real CSV output (no MRF needed)
      real.py           orchestrates the adapted extract_tic_all + enrich_npi_all
      package.py        CSV split at CSV_ROW_THRESHOLD + zip
  schema.sql            MySQL schema for hand-provisioning
  tests/test_smoke.py   full upload→worker→zip→KPI flow, no infra
frontend/
  src/pages/            Upload · Jobs · JobDetail (polling) · Dashboard
  src/components/        AuroraBackground, GlassCard, ProgressBar, StatusBadge, charts…
deploy/                 systemd units + nginx site for 172.22.6.98 (see DEPLOY.md)
scripts/dev.ps1 / .sh   one-command local dev (API + worker + Vite)
```

## Quick start (local, zero infra)

**Prereqs:** Python 3.11+ (tested on 3.14), Node 18+.

```bash
# Windows
./scripts/dev.ps1
# Linux / macOS / Git-Bash
./scripts/dev.sh
```

That creates the venv, installs deps, writes `backend/.env` (SQLite + **stub**
engine), creates tables, and starts:

* API  → http://localhost:8000  (`/docs` for Swagger)
* UI   → http://localhost:5173

With `EXTRACTION_MODE=stub` you can upload *any* `.json`/`.json.gz` (even a
1-line file) and watch the whole pipeline — progress bar, stages, CSV zip,
KPI numbers — without a multi-GB payer file. Optionally seed demo history:

```bash
cd backend && .venv/Scripts/python -m scripts.seed_demo   # ./ .venv/bin/python on *nix
```

### Manual start (equivalent)

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python -m scripts.init_db
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000   # terminal 1
.venv/Scripts/python -m app.worker                                  # terminal 2

cd ../frontend && npm install && npm run dev                        # terminal 3
```

## Switching on the real extraction engine

Set `EXTRACTION_MODE=real` in `backend/.env` (it's the default in
`.env.example`). The worker then runs the adapted `extract_tic_all.py` +
`enrich_npi_all.py`:

1. **Building provider lookup** — `ijson.parse` over `provider_references`, mapping `provider_group_id → [{tin, npis}]` (resolves external `location` URLs too).
2. **Extracting rates** — `ijson` stream over `in_network`, one row per *(negotiated price × provider sub-group)* for the 81 target E&M + ophthalmic codes; any sub-group where an NPI equals its own TIN is dropped as corrupt.
3. **Enriching NPIs** — first `NPI_MAX_PER_ROW` (5) NPIs per row looked up against the NPPES registry across `NPI_LOOKUP_THREADS` (8) threads, cached; adds `provider_names`, `organization_names`, address columns.
4. **Packaging** — rows written to `part_0001.csv…` rolling at `CSV_ROW_THRESHOLD` (500k) rows, plus a `manifest.json`, all zipped.

The two scripts still work standalone (`python extract_tic_all.py in.json.gz out.csv`);
the worker just imports their generator entrypoints (`iter_rate_rows`,
`iter_enriched_rows`) so nothing is duplicated.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs/uploads` | begin a chunked upload → `{upload_id, chunk_size}` |
| `POST` | `/api/jobs/uploads/{id}/chunks` | append one part (multipart `index` + `chunk`) |
| `POST` | `/api/jobs/uploads/{id}/complete` | assemble → create job (queued) |
| `POST` | `/api/jobs` | single-shot upload for small files (multipart) |
| `GET`  | `/api/jobs` | list + filter (`company`, `status`, `date_from`, `date_to`, `limit`, `offset`) |
| `GET`  | `/api/jobs/{id}` | full detail |
| `GET`  | `/api/jobs/{id}/status` | lightweight poll — `{status, percent, eta_seconds, stage, …}` |
| `GET`  | `/api/jobs/{id}/download` | stream the output zip |
| `GET`  | `/api/kpis` | dashboard aggregates (live SQL) |
| `GET`  | `/api/companies` · `/api/meta` | payer list + runtime config |

Frontend polls `/status` every 3.5s while `queued`/`processing`, stops on
`completed`/`failed`.

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest -q
```

Spins a throwaway SQLite DB + temp data dir, forces stub mode, and drives a
job through the real HTTP endpoints and worker code path (claim → pipeline →
complete), asserting the zip contents and KPI math. No MySQL/Redis/MRF needed.

## Config reference

See [`backend/.env.example`](backend/.env.example) — every knob (DB URL, data
root, CORS, chunk size, upload ceiling, extraction mode, worker poll/stuck
timeouts, CSV threshold, NPI threads) is there with comments.

## Deploying to 172.22.6.98

See [`DEPLOY.md`](DEPLOY.md) — systemd units for the API and worker, nginx site
(static SPA + `/api` proxy), MySQL provisioning, and the update procedure.
