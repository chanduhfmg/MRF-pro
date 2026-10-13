"""/api/kpis + /api/companies + /api/meta."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import crud
from ..config import settings
from ..constants import INSURANCE_COMPANIES
from ..database import get_db
from ..schemas import (
    CompanyCount,
    CompanyOut,
    JobOut,
    KpiOut,
    KpiSnapshotOut,
    KpiTotals,
    VolumePoint,
)

log = logging.getLogger("mrf.kpis")

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/companies", response_model=list[CompanyOut])
def companies() -> list[CompanyOut]:
    return [CompanyOut(**c) for c in INSURANCE_COMPANIES]


@router.get("/meta")
def meta() -> dict:
    return {
        "extraction_mode": settings.extraction_mode,
        "upload_chunk_size": settings.upload_chunk_size,
        "max_upload_bytes": settings.max_upload_bytes,
        "csv_row_threshold": settings.csv_row_threshold,
        "companies": INSURANCE_COMPANIES,
        "search_max_files": settings.search_max_files,
        "search_concurrency": settings.search_concurrency,
        "search_probe_max_bytes": settings.search_probe_max_bytes,
    }


@router.get("/kpis", response_model=KpiOut)
def kpis(db: Session = Depends(get_db)) -> KpiOut:
    totals = crud.kpi_totals(db)
    by_company = crud.kpi_by_company(db)
    volume = crud.kpi_volume_over_time(db, days=30)
    queue_eta = crud.queue_eta_seconds(db)
    recent, _ = crud.list_jobs(db, limit=8, offset=0)

    # Persist what we just computed (throttled inside record_kpi_snapshot, so
    # a polling dashboard writes one row every few minutes, not one per poll).
    # A snapshot is bookkeeping: if MySQL refuses the INSERT the dashboard
    # must still render, so a failure here is logged and swallowed.
    try:
        crud.record_kpi_snapshot(
            db,
            totals=totals,
            by_company=by_company,
            volume=volume,
            queue_eta_seconds=queue_eta,
            source="api",
        )
    except Exception:
        db.rollback()
        log.warning("KPI snapshot not recorded", exc_info=True)

    return KpiOut(
        totals=KpiTotals(**totals),
        by_company=[CompanyCount(**c) for c in by_company],
        volume_over_time=[VolumePoint(**v) for v in volume],
        recent_jobs=[JobOut.from_job(j) for j in recent],
        queue_eta_seconds=queue_eta,
    )


@router.get("/kpis/history", response_model=list[KpiSnapshotOut])
def kpi_history(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(500, ge=1, le=5000),
    source: str | None = Query(None, pattern="^(api|worker|manual)$"),
    db: Session = Depends(get_db),
) -> list[KpiSnapshotOut]:
    """Stored KPI snapshots, oldest first - the trend behind the live tiles."""
    rows = crud.list_kpi_snapshots(db, days=days, limit=limit, source=source)
    return [KpiSnapshotOut.from_snapshot(r) for r in rows]


@router.post("/kpis/snapshot", response_model=KpiSnapshotOut)
def kpi_snapshot_now(db: Session = Depends(get_db)) -> KpiSnapshotOut:
    """Force a capture right now, ignoring the throttle."""
    snap = crud.capture_kpi_snapshot(db, source="manual", force=True)
    return KpiSnapshotOut.from_snapshot(snap)
