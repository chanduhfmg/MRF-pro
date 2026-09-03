"""
Data-access layer: job queries, the atomic "claim next queued job" used by
the polling worker, and the aggregate KPI queries (computed live, no
pre-aggregated table).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta  # noqa: F401  (datetime used in annotations)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .constants import company_label
from .models import ExtractionJob, JobStatus
from .utils import utcnow


# --------------------------------------------------------------------------
# Job CRUD
# --------------------------------------------------------------------------

def create_job(
    db: Session,
    *,
    insurance_company: str,
    source_filename: str,
    source_file_path: str,
    total_bytes: int | None,
) -> ExtractionJob:
    job = ExtractionJob(
        id=str(uuid.uuid4()),
        insurance_company=insurance_company,
        source_filename=source_filename,
        source_file_path=source_file_path,
        total_bytes=total_bytes,
        status=JobStatus.queued,
        current_stage="Queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str) -> ExtractionJob | None:
    return db.get(ExtractionJob, job_id)


def list_jobs(
    db: Session,
    *,
    company: str | None = None,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExtractionJob], int]:
    conds = []
    if company:
        conds.append(ExtractionJob.insurance_company == company)
    if status:
        conds.append(ExtractionJob.status == status)
    if date_from:
        conds.append(ExtractionJob.created_at >= date_from)
    if date_to:
        conds.append(ExtractionJob.created_at <= date_to)

    base = select(ExtractionJob)
    for c in conds:
        base = base.where(c)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        db.scalars(
            base.order_by(ExtractionJob.created_at.desc()).limit(limit).offset(offset)
        )
    )
    return rows, total


# --------------------------------------------------------------------------
# Worker-facing operations
# --------------------------------------------------------------------------

def claim_next_job(db: Session, worker_id: str) -> ExtractionJob | None:
    """Atomically move the oldest queued job to 'processing' and return it.

    MySQL 8: uses SELECT ... FOR UPDATE SKIP LOCKED so multiple workers never
    grab the same row. SQLite: single-writer, a plain transaction is enough.
    """
    stmt = (
        select(ExtractionJob)
        .where(ExtractionJob.status == JobStatus.queued)
        .order_by(ExtractionJob.created_at.asc())
        .limit(1)
    )
    if not settings.is_sqlite:
        stmt = stmt.with_for_update(skip_locked=True)

    job = db.scalars(stmt).first()
    if job is None:
        db.rollback()
        return None

    job.status = JobStatus.processing
    job.worker_id = worker_id
    job.started_at = job.started_at or utcnow()
    job.current_stage = "Preparing"
    db.commit()
    db.refresh(job)
    return job


def requeue_stuck_jobs(db: Session) -> int:
    """Any job 'processing' whose heartbeat is older than WORKER_STUCK_TIMEOUT
    is considered dead. Requeue it, or fail it once it has burned through
    WORKER_MAX_RETRIES. Never leaves a job silently stuck."""
    cutoff = utcnow() - timedelta(seconds=settings.worker_stuck_timeout)
    stuck = list(
        db.scalars(
            select(ExtractionJob).where(
                ExtractionJob.status == JobStatus.processing,
                ExtractionJob.updated_at < cutoff,
            )
        )
    )
    for job in stuck:
        if job.retry_count >= settings.worker_max_retries:
            job.status = JobStatus.failed
            job.completed_at = utcnow()
            job.error_message = (
                job.error_message
                or f"Worker stopped responding (no heartbeat for "
                f">{settings.worker_stuck_timeout}s); retries exhausted."
            )
        else:
            job.retry_count += 1
            job.status = JobStatus.queued
            job.worker_id = None
            job.current_stage = "Queued (requeued after stall)"
            job.bytes_processed = 0
            job.records_processed = 0
    if stuck:
        db.commit()
    return len(stuck)


def update_progress(
    db: Session,
    job_id: str,
    *,
    stage: str | None = None,
    bytes_processed: int | None = None,
    records_processed: int | None = None,
    total_bytes: int | None = None,
) -> None:
    job = db.get(ExtractionJob, job_id)
    if not job:
        return
    if stage is not None:
        job.current_stage = stage
    if bytes_processed is not None:
        job.bytes_processed = bytes_processed
    if records_processed is not None:
        job.records_processed = records_processed
    if total_bytes is not None:
        job.total_bytes = total_bytes
    job.updated_at = utcnow()
    db.commit()


def mark_completed(
    db: Session,
    job_id: str,
    *,
    output_zip_path: str,
    output_size_bytes: int,
    record_count: int,
    output_file_count: int,
) -> None:
    job = db.get(ExtractionJob, job_id)
    if not job:
        return
    job.status = JobStatus.completed
    job.completed_at = utcnow()
    job.current_stage = "Completed"
    job.output_zip_path = output_zip_path
    job.output_size_bytes = output_size_bytes
    job.record_count = record_count
    job.records_processed = record_count
    job.output_file_count = output_file_count
    if job.total_bytes:
        job.bytes_processed = job.total_bytes
    db.commit()


def mark_failed(db: Session, job_id: str, error_message: str) -> None:
    job = db.get(ExtractionJob, job_id)
    if not job:
        return
    job.status = JobStatus.failed
    job.completed_at = utcnow()
    job.error_message = (error_message or "Unknown error")[:60000]
    db.commit()


# --------------------------------------------------------------------------
# KPI aggregates (live SQL, no pre-computed table)
# --------------------------------------------------------------------------

def kpi_totals(db: Session) -> dict:
    total_jobs = db.scalar(select(func.count(ExtractionJob.id))) or 0
    total_records = db.scalar(select(func.coalesce(func.sum(ExtractionJob.record_count), 0))) or 0
    total_storage = db.scalar(
        select(func.coalesce(func.sum(ExtractionJob.output_size_bytes), 0))
    ) or 0

    def _count(st: JobStatus) -> int:
        return db.scalar(
            select(func.count(ExtractionJob.id)).where(ExtractionJob.status == st)
        ) or 0

    completed = _count(JobStatus.completed)
    failed = _count(JobStatus.failed)
    processing = _count(JobStatus.processing)
    queued = _count(JobStatus.queued)
    finished = completed + failed
    success_rate = (completed / finished) if finished else 0.0

    return {
        "total_jobs": total_jobs,
        "total_records": int(total_records),
        "total_storage_bytes": int(total_storage),
        "success_rate": round(success_rate, 4),
        "completed_jobs": completed,
        "failed_jobs": failed,
        "processing_jobs": processing,
        "queued_jobs": queued,
    }


def kpi_by_company(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            ExtractionJob.insurance_company,
            func.count(ExtractionJob.id),
            func.coalesce(func.sum(ExtractionJob.record_count), 0),
        ).group_by(ExtractionJob.insurance_company)
    ).all()
    out = [
        {
            "company": r[0],
            "company_label": company_label(r[0]),
            "jobs": int(r[1]),
            "records": int(r[2]),
        }
        for r in rows
    ]
    out.sort(key=lambda x: x["jobs"], reverse=True)
    return out


def kpi_volume_over_time(db: Session, days: int = 30) -> list[dict]:
    """Jobs + records grouped by calendar day (created_at)."""
    since = utcnow() - timedelta(days=days)
    # DATE() works on both MySQL and SQLite.
    day = func.date(ExtractionJob.created_at)
    rows = db.execute(
        select(
            day,
            func.count(ExtractionJob.id),
            func.coalesce(func.sum(ExtractionJob.record_count), 0),
        )
        .where(ExtractionJob.created_at >= since)
        .group_by(day)
        .order_by(day.asc())
    ).all()

    by_day = {str(r[0]): (int(r[1]), int(r[2])) for r in rows}
    out: list[dict] = []
    for i in range(days, -1, -1):
        d = (utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        jobs, records = by_day.get(d, (0, 0))
        out.append({"date": d, "jobs": jobs, "records": records})
    return out
