"""
FastAPI application entrypoint.

    uvicorn app.main:app --reload            (local dev)
    uvicorn app.main:app --host 0.0.0.0 --port 8000   (server 172.22.6.98)

The background worker runs as a SEPARATE process (`python -m app.worker`),
not inside this one - extraction never touches the request path.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .routers import jobs, kpis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("mrf.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    init_db()
    log.info(
        "API up | db=%s | mode=%s | data_root=%s",
        "sqlite" if settings.is_sqlite else "mysql",
        settings.extraction_mode,
        settings.data_root_path,
    )
    yield


app = FastAPI(
    title="Insurance MRF Extraction Dashboard API",
    version="0.1.0",
    description="Automates extracting & packaging Transparency-in-Coverage MRFs.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(kpis.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "sqlite" if settings.is_sqlite else "mysql",
        "extraction_mode": settings.extraction_mode,
    }


@app.exception_handler(Exception)
async def _unhandled(_req, exc: Exception):  # noqa: ANN001
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --- Optionally serve the built frontend (server deployment) ---------------
# Set FRONTEND_DIST=/path/to/frontend/dist to have FastAPI serve the SPA.
if settings.frontend_dist:
    dist = Path(settings.frontend_dist)
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")
        log.info("Serving frontend from %s", dist)
    else:
        log.warning("FRONTEND_DIST=%s is not a directory; not serving SPA", dist)
