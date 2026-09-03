"""/api/kpis + /api/companies + /api/meta."""
from __future__ import annotations

from fastapi import APIRouter, Depends
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
    KpiTotals,
    VolumePoint,
)

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
    }


@router.get("/kpis", response_model=KpiOut)
def kpis(db: Session = Depends(get_db)) -> KpiOut:
    totals = crud.kpi_totals(db)
    by_company = crud.kpi_by_company(db)
    volume = crud.kpi_volume_over_time(db, days=30)
    recent, _ = crud.list_jobs(db, limit=8, offset=0)

    return KpiOut(
        totals=KpiTotals(**totals),
        by_company=[CompanyCount(**c) for c in by_company],
        volume_over_time=[VolumePoint(**v) for v in volume],
        recent_jobs=[JobOut.from_job(j) for j in recent],
    )
