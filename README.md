# Insurance MRF Extraction Dashboard

Internal tool for **Transparency-in-Coverage (TiC) machine-readable files
(MRFs)** from UHC, Aetna, Anthem, Cigna and MVP.

The main flow is **search-first**: pick a payer, search its published MRFs by
**NPI or TIN**, and the tool tells you exactly which in-network files contain
that provider. Choose how many of those files to run, and each one is
downloaded, extracted for the target CPT/HCPCS codes and enriched against NPPES
**automatically** - one decision, no further steps. Every stage reports live
progress and an ETA.

```
Pick payer -> Search NPI/TIN -> Matching files -> Select N -> Download -> Extract -> Enrich -> Zip
                    |                                        \_________ automatic _________/
                    \__ payer index URL is hardcoded per payer
```

Uploading a `.json.gz` you already have still works, and lands in the same
pipeline.

## The search flow

Payer index (table-of-contents) URLs are **hardcoded per payer** in
[`backend/app/payers.py`](backend/app/payers.py). They are stored as
*date-templated* patterns rather than literals because every payer re-publishes
monthly with the reporting date in the path:

```
https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com/anthem/{ymd}_anthem_index.json.gz
```

The resolver expands each template over recent months and keeps the first URL
that responds **and** actually serves JSON - several payer portals are SPAs that
answer `200` with an HTML shell for any path, which would otherwise send the
search off to parse a web page.

Three resolver kinds, because payers publish three different ways:

| Payer | How the index is found | Status |
|---|---|---|
| **UHC** | `templates` - blob-listing API, one flat list of every file, sizes inline | live |
| **Anthem** | `templates` - dated S3 table of contents (~10 GB gzipped) | live |
| **Cigna** | `lookup` - the page's own `/static/mrf/latest.json` names a signed CloudFront TOC; re-read each time because the signature expires | live |
| Aetna | `healthsparq` - `mrf.healthsparq.com/aetnacvs-egress…/AETNACVS_I/{brand}/{date}/tableOfContents/…_index.json.gz`, one file per legal entity, posted on the 5th | **blocked** (see below) |
| MVP | `healthsparq` - same host, `MVP_I/MVP/…` | **blocked** (see below) |

**Aetna and MVP: blocked at the firewall, not a wrong URL.** Both outsource
publishing to HealthSparq/Kyruus, and `mrf.healthsparq.com` sits behind an
Imperva WAF that refuses the TLS handshake (`TLSV1_ALERT_ACCESS_DENIED`) to
automated clients. We confirmed this from this network *and* from an
unrelated cloud network, with Python, Windows `curl`, and a Chrome-impersonating
TLS client - every path pattern fails identically before any HTTP status is
returned. The resolver detects that alert, stops probing the host after the
first refusal, and reports `blocked=true` with the host name; the Search page
shows it under the payer selector *before* you click Search. If the publisher
page opens in your browser and you can download the table of contents there,
pin a URL this server can reach (an internal mirror). Otherwise those two
payers cannot be searched from here, and no code change will alter that.

**Pinning a URL** - for the month a payer moves its bucket, or to point a
blocked payer at a mirror. The Search page's index panel has a *Pin index URL*
box; it verifies the URL serves JSON (or gzip) before saving, and refuses an
HTML shell or a blocked host with the reason. Equivalent API:
`POST /api/payers/{key}/index` `{"index_url": "..."}` (`DELETE` unpins), which
persists to `backend/payer_sources.json`:

```json
{ "aetna": { "index_url": "https://mirror.internal/aetna/2026-09-05_index.json.gz" } }
```

`GET /api/payers?probe=true` shows every payer's status; `GET /api/payers/{key}`
is the single-payer check the UI uses.

### How a file is searched for an NPI/TIN

A payer index can reference millions of multi-GB files, so "search everything"
does not exist. The search is bounded and says so: it ranks candidates, scans
the top `max_files` of them, and reports *"3 matches in 40 files scanned"*
rather than implying the payer was exhaustively searched.

Each file is checked in two tiers, cheapest first:

1. **Byte scan** - stream-decompress and look for the id's digits with
   digit-boundary anchoring, so a 10-digit NPI can't match inside a longer
   number. Far faster than JSON parsing, works wherever in the file the id
   lives, and aborts the instant it hits.
2. **Context confirm** - regex the enclosing `tin` / `provider_group_id` /
   `npi` fragment out of the bytes around the hit. That turns "these digits
   appear" into "this id sits in a provider group, here's its TIN". A hit with
   no provider context is reported as `probable`, never silently upgraded.

Ranking spends the budget where a hit is plausible: substantial files that fit
the byte budget first (bigger = broader network), then unknown-size, then
over-budget files, and stub files (payers publish thousands of ~1 KB
placeholders) last.

**What "Files to scan" means.** The payer's index lists thousands of
in-network files (UHC: ~6,900; Anthem: millions of references). Every file that
gets *scanned* has to be downloaded and read, at hundreds of MB each - so the
search does not open all of them. "Files to scan" is the cap: how many of the
ranked candidates get opened and checked for your NPI/TIN before the search
declares itself done. `40` is the default; raise it for a more thorough search
(slower), lower it for a quick look. Because the likeliest files are scanned
first, hits tend to arrive early - which is why you can stop the scan the moment
you have what you need instead of waiting for the cap.

**Results arrive live.** Matching files appear in a paginated table under the
progress bar *while the scan is still running*, each with its own
**Start extraction** button. You do not wait for the whole scan to finish.

**Stop process.** Once the files you wanted have shown up, click **Stop
process**. In-flight probes abort at their next chunk, queued ones are dropped,
and the search ends as `cancelled` (not `failed`) with every match it found
still listed and extractable. The progress bar freezes where it was - a stopped
search never claims 100%.

**Choosing the CPT/HCPCS codes.** The *Extraction settings* card decides what
each "Start extraction" pulls. Leave the box blank for the built-in default set
(78 E&M + ophthalmic codes), or paste your own - `99213, 99214, G0439` in any
separator. **Save as set…** names the list ("E&M codes", "Cardiology",
"Ophthalmology") so it can be picked from the dropdown next time; sets are
shared by everyone using the dashboard. A custom list *replaces* the default,
it does not add to it. Every field the payer file carries is kept regardless;
anything outside the fixed CSV header lands in the `extra_fields` JSON column.

**Narrowing to the provider.** By default the extracted CSVs keep only rows for
the NPI/TIN you searched. On a real UHC Oxford file that is 224 rows kept and
552,708 off-target rows dropped (or 10 rows with a 4-code custom set). Uncheck
the box to get every provider group in the file instead.

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
extract_tic_all.py      real extraction logic (ijson 2-pass, NPI==TIN guard, optional
                        target NPI/TIN filter) — adapted, still runs standalone
enrich_npi_all.py       real NPPES enrichment (threaded) — adapted, still runs standalone
backend/
  app/
    main.py             FastAPI app + routers + optional SPA hosting
    config.py           all env-driven settings (.env)
    payers.py           ** hardcoded MRF index URLs per payer ** + live resolver
    eta.py              EWMA rate estimator + phase-weighted pipeline ETA
    net.py              streaming HTTP with transparent gzip + byte counting
    mrf_index.py        TOC readers: CMS reporting_structure, UHC blob listing
    provider_probe.py   two-tier NPI/TIN membership test for one in-network file
    models.py           ExtractionJob · ProviderSearch · SearchMatch
    crud.py             queries, atomic claim (both queues), KPI aggregates
    routers/jobs.py     chunked upload, list/detail/status, zip download
    routers/search.py   /api/payers, /api/searches, select → extract
    routers/kpis.py     /api/kpis, /api/companies, /api/meta
    worker.py           polling worker — searches first, then extractions
    pipeline/
      run.py            dispatch: stub | real
      stub.py           fake progress + real CSV output (no MRF needed)
      real.py           download → lookup → extract → enrich → package
      search.py         resolve index → list files → rank → probe in parallel
      package.py        CSV split at CSV_ROW_THRESHOLD + zip
  schema.sql            MySQL schema for hand-provisioning
  payer_sources.json    optional: pin/override payer index URLs (gitignored)
  tests/test_smoke.py   full upload→worker→zip→KPI flow, no infra
  tests/test_search.py  search→select→extract flow, network stubbed at one seam
frontend/
  src/pages/            Search · Upload · Jobs · JobDetail (polling) · Dashboard
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

That creates the venv, installs deps, writes `backend/.env` (SQLite + the
**real** engine), creates tables, and starts:

* API    → http://localhost:8000  (`/docs` for Swagger)
* UI     → http://localhost:5173
* Search → http://localhost:5173/search  ← start here

Pass `-Stub` for the simulated engine - useful for UI work without pulling
multi-GB payer files.

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
| `GET`  | `/api/payers` | hardcoded MRF sources (`?probe=true` to check liveness) |
| `POST` | `/api/payers/{key}/index` | pin an exact index URL (persists to `payer_sources.json`) |
| `POST` | `/api/searches` | queue an NPI/TIN search |
| `GET`  | `/api/searches/{id}/status` | poll — `{percent, eta_seconds, files_scanned, files_matched, …}` |
| `GET`  | `/api/searches/{id}/matches` | one page of files examined (`limit`, `offset`, `matched_only`), matches first, with size + time estimates; safe to poll mid-scan |
| `POST` | `/api/searches/{id}/cancel` | **Stop process** — end the scan early, keep every match found |
| `POST` | `/api/searches/{id}/extract` | turn matches into queued jobs (`match_ids` or `top_n`; optional `codes[]` or `code_set_id`) |
| `GET`  | `/api/searches/{id}/jobs` | the jobs one search produced |
| `GET`  | `/api/code-sets` | saved CPT/HCPCS code sets |
| `POST` | `/api/code-sets` | save a named set `{name, codes[]}` (409 if the name exists) |
| `PUT`  | `/api/code-sets/{id}` · `DELETE` | rename / replace codes · delete |
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

Frontend polls `/api/jobs/{id}/status` every 3.5s and
`/api/searches/{id}/status` every 2s while `queued`/`processing`, stopping on
`completed`/`failed`.

## ETA everywhere, as a countdown

Every long-running operation reports one, and the UI **ticks it down every
second** (`4:32 left`, `4:31 left`…) rather than showing a number that only
moves when the next poll lands. A fresh server estimate re-anchors the clock;
if it agrees with where the countdown already is, the anchor is kept so the
display never jumps *up* just because the poll repeated itself.

| Where | Basis |
|---|---|
| Upload | client-side bytes/sec - only the browser knows the real uplink |
| Search | fractional files scanned, **including partly-read files** |
| Download | `Content-Length` vs bytes written, plus a live MB/s |
| Extract -> enrich -> package | time-based phase plan, see below |
| Dashboard | queue-drain: live ETAs of running jobs + median recent duration x queued |

**Why the pipeline ETA is time-based.** Fixed phase weights ("download is 22%
of the job") are wrong for nearly every real run: a narrowed extraction barely
touches NPPES, a whole-network one spends hours there. So each phase is given
an estimated *duration* from the file size and the throughput observed on
earlier jobs (`data/_cache/phase_rates.json`, learned separately for narrowed
vs whole-network runs), the running phase is projected from its own live rate,
and percent is time-proportional. Progress is clamped monotonic - it cannot
jump backwards the way the old model did (94% -> 56% on a real job).

**Why the moving average is time-weighted.** `RateEstimator` decays with
`alpha = 1 - exp(-dt/tau)`, so a sample covering ten minutes outweighs one
covering ten seconds. Sample-counted decay let one fast measurement from the
first minute keep the estimate optimistic through an hour of crawling - a job
that slows down now reports a *growing* ETA, which is the honest answer.

The search ETA counts partial files deliberately: with several 383 MB files
probed in parallel, whole-file counting pins the bar at 0% and then jumps to
100% - exactly the moment an ETA is worth having.

## Provider details: every NPI, not the first five

Enrichment resolves **every NPI on every row** against NPPES (`NPI_MAX_PER_ROW=0`).
The standalone script capped this at 5 per row to bound runtime, which quietly
left most providers in a large group with no name, address or ZIP at all.
Unique NPIs are cached, so a provider costs exactly one lookup however many
rows reference it.

Each rate row carries per-NPI lists, `;`-joined and index-aligned with the
`npis` column:

| column | what it holds |
|---|---|
| `provider_zips` | ZIP exactly as NPPES returns it (usually ZIP+4, 9 digits) |
| `provider_zip5s` | the same ZIPs normalized to 5 digits, one per NPI |
| `primary_zip` | the row's first non-empty 5-digit ZIP, as a single value |
| `provider_names` · `organization_names` · `provider_addresses` · `provider_cities` · `provider_states` | same shape |

`primary_zip` is a convenience for grouping, **not** the only ZIP - it never
replaces the full list.

Splitting those lists to answer "which provider is at 07202?" is still
awkward, so the same data is written **one line per NPI** as well:
`providers.csv` in the zip, and a `providers` sheet in the workbook -
`npi, provider_name, organization_name, address, city, state, zip, zip5,
tins, row_count`. Filter that, then join back on `npi`.

## Downloads: one Excel file, named so you can tell what it is

The default download is a single **.xlsx** (sheet `rates` = every row,
sheet `summary` = what was extracted and why), named:

```
{PAYER}_{plan}_{TIN|NPI+id | ALL}_{YYYYMMDD}_{codeset}.xlsx

UHC_Oxford-Metro-27_NPI1013988054_20260914_Ophthalmology.xlsx
CIGNA_OAP_TIN473943680_20260914_default.xlsx
ANTHEM_Blue-Access_ALL_20260914_custom14.xlsx
```

Plan names are shortened by dropping legal boilerplate (*Inc, Insurer, Third
Party Administrator, Health, Plan…*) and keeping the first three meaningful
words. `ALL` means the extraction was not narrowed to the searched provider;
`default` is the built-in code set, a saved set uses its name, an ad-hoc list
shows its count. NPIs, TINs and ZIPs are written as text so leading zeros
survive Excel; rates are numeric.

**Row limit.** Excel holds 1,048,576 rows per sheet. Above ~1M rows no
workbook is written and the CSV zip (`?format=zip`, always available) is the
deliverable; the UI says so instead of offering a truncated file. Jobs that
finished before this feature get their workbook built on first download from
the CSV parts already on disk.

## Choosing scope, and stopping

Every **Start extraction** asks one question first: **only the searched
TIN/NPI** (recommended - minutes, a few hundred rows) or **every provider that
bills these codes** (the whole network - hundreds of thousands of rows, hours
of NPPES enrichment). The difference is three orders of magnitude, so it is a
decision, not a hidden default; the answer is remembered for the next one.

**Stop extraction** (job page and Jobs list) aborts a running job within a few
seconds and discards its partial output; a queued job is simply removed. The
job ends as *Stopped*, never *Failed*. **Stop process** does the same for a
search, keeping every match already found.

**Why zero rows?** A completed job with no rows shows exactly which of your
choices produced that: the provider was found but bills none of the chosen
codes here, which codes other providers do bill in the file, which codes are
absent from the file altogether - and what to re-run.

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
