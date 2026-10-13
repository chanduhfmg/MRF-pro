"""
Data-access layer: job queries, the atomic "claim next queued job" used by
the polling worker, and the aggregate KPI queries.

KPIs are still *computed live* off `extraction_jobs` - nothing reads from a
pre-aggregated table, so a number on the dashboard is never stale. They are
additionally *persisted* to `kpi_snapshots` (see record_kpi_snapshot), which
is a history for trending, not a cache to read the live values back out of.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta  # noqa: F401  (datetime used in annotations)

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import settings
from .constants import company_label
from .models import (
    CptCodeSet,
    ExtractionJob,
    JobStatus,
    KpiSnapshot,
    ProviderSearch,
    SearchMatch,
    SearchType,
)
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
    source_url: str | None = None,
    search_id: str | None = None,
    match_id: str | None = None,
    target_type: str | None = None,
    target_value: str | None = None,
    filter_to_target: bool = False,
    target_codes: str | None = None,
    plan_name: str | None = None,
    code_set_name: str | None = None,
) -> ExtractionJob:
    job = ExtractionJob(
        id=str(uuid.uuid4()),
        insurance_company=insurance_company,
        source_filename=source_filename,
        source_file_path=source_file_path,
        total_bytes=total_bytes,
        status=JobStatus.queued,
        current_stage="Queued",
        source_url=source_url,
        search_id=search_id,
        match_id=match_id,
        target_type=target_type,
        target_value=target_value,
        filter_to_target=filter_to_target,
        target_codes=target_codes,
        plan_name=(plan_name or None),
        code_set_name=(code_set_name or None),
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
    grab the same row.

    SQLite has no row locking, so a plain SELECT-then-UPDATE is NOT safe with
    more than one worker process: two workers can both SELECT the same
    'queued' row before either COMMITs, and both then run the same job -
    exactly the bug this replaced (two worker processes were found running
    the identical search concurrently, corrupting its progress and leaving
    neither one free to pick up other queued work). The fix is a single
    atomic UPDATE ... WHERE status='queued', gated by its rowcount: SQLite
    serializes writes one COMMIT at a time regardless of how many workers
    race for it, so whichever UPDATE lands first flips the row to
    'processing' and every other worker's UPDATE for that same id then
    matches zero rows and knows it lost the race.
    """
    if settings.is_sqlite:
        candidate_id = db.scalars(
            select(ExtractionJob.id)
            .where(ExtractionJob.status == JobStatus.queued)
            .order_by(ExtractionJob.created_at.asc())
            .limit(1)
        ).first()
        if candidate_id is None:
            db.rollback()
            return None

        result = db.execute(
            update(ExtractionJob)
            .where(ExtractionJob.id == candidate_id, ExtractionJob.status == JobStatus.queued)
            .values(status=JobStatus.processing, worker_id=worker_id, current_stage="Preparing")
        )
        db.commit()
        if result.rowcount == 0:
            return None  # another worker's claim landed first

        job = db.get(ExtractionJob, candidate_id)
        if job.started_at is None:
            job.started_at = utcnow()
            db.commit()
            db.refresh(job)
        return job

    stmt = (
        select(ExtractionJob)
        .where(ExtractionJob.status == JobStatus.queued)
        .order_by(ExtractionJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
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
    downloaded_bytes: int | None = None,
    eta_seconds: float | None = None,
    throughput_bps: float | None = None,
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
    if downloaded_bytes is not None:
        job.downloaded_bytes = downloaded_bytes
    if eta_seconds is not None:
        job.eta_seconds_est = eta_seconds
    if throughput_bps is not None:
        job.throughput_bps = throughput_bps
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
    output_xlsx_path: str | None = None,
    requested_code_count: int | None = None,
    matched_code_count: int | None = None,
    matched_codes: list[str] | None = None,
) -> None:
    import json

    job = db.get(ExtractionJob, job_id)
    if not job:
        return
    job.status = JobStatus.completed
    job.completed_at = utcnow()
    job.current_stage = "Completed"
    job.output_zip_path = output_zip_path
    job.output_xlsx_path = output_xlsx_path
    job.output_size_bytes = output_size_bytes
    job.record_count = record_count
    job.records_processed = record_count
    job.output_file_count = output_file_count
    job.requested_code_count = requested_code_count
    job.matched_code_count = matched_code_count
    job.matched_codes_json = json.dumps(matched_codes) if matched_codes is not None else None
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


def rerun_job(db: Session, job_id: str) -> ExtractionJob | None:
    """Retry a failed job - a firewall blip, a transient network error,
    whatever it was. Reuses the same row (same id, same search/match
    linkage, same source file/codes/target) rather than creating a
    duplicate: a rerun IS this job, tried again, not a new one queued
    alongside the dead one. Only `failed` jobs are eligible - queued/
    processing has nothing to retry, and completed/cancelled are handled
    elsewhere (download it again / it's already gone)."""
    job = db.get(ExtractionJob, job_id)
    if not job or job.status != JobStatus.failed:
        return None
    job.status = JobStatus.queued
    job.current_stage = "Queued (retry)"
    job.worker_id = None
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    job.bytes_processed = 0
    job.records_processed = 0
    job.downloaded_bytes = 0
    job.output_zip_path = None
    job.output_xlsx_path = None
    job.output_size_bytes = None
    job.record_count = None
    job.output_file_count = None
    job.requested_code_count = None
    job.matched_code_count = None
    job.matched_codes_json = None
    job.retry_count = 0
    job.cancel_requested = False
    job.eta_seconds_est = None
    job.throughput_bps = None
    db.commit()
    db.refresh(job)
    return job


def delete_job(db: Session, job_id: str) -> bool:
    """Permanently remove a job row. A stopped job never has output worth
    keeping, so rather than leave a dead 'cancelled' row sitting in the
    list forever, stopping a job deletes it outright. Any search match
    that was pointing at this job as its queued/completed extraction is
    unlinked first, so it goes back to looking un-extracted (and
    re-extractable) instead of referencing a job that no longer exists."""
    job = db.get(ExtractionJob, job_id)
    if not job:
        return False
    db.execute(
        update(SearchMatch).where(SearchMatch.job_id == job_id).values(job_id=None)
    )
    db.delete(job)
    db.commit()
    return True


def request_job_cancel(db: Session, job_id: str) -> tuple[ExtractionJob | None, bool]:
    """"Stop extraction" from the UI. Returns (job, deleted).

    A queued job has no worker to tell and nothing produced yet, so it is
    deleted immediately - `job` comes back None, `deleted` True. A running
    one gets the flag instead; the worker's progress reporter reads it on
    its next write (every couple of seconds), aborts the pipeline, and
    deletes the job itself (see mark_job_cancelled) once it has discarded
    the partial output - `job` comes back with `current_stage` updated so
    the caller can show "Stopping…" in the meantime.
    """
    job = db.get(ExtractionJob, job_id)
    if not job:
        return None, False
    if job.status == JobStatus.queued:
        delete_job(db, job_id)
        return None, True
    if job.status == JobStatus.processing:
        job.cancel_requested = True
        job.current_stage = "Stopping…"
        db.commit()
        db.refresh(job)
    return job, False


def is_job_cancel_requested(db: Session, job_id: str) -> bool:
    job = db.get(ExtractionJob, job_id)
    return bool(job and job.cancel_requested)


def mark_job_cancelled(db: Session, job_id: str) -> None:
    """Terminal state for a job the user stopped: deleted outright rather
    than left sitting in the list as a dead 'cancelled' row. The caller
    (worker.py) has already discarded any partial output before calling
    this, so there is nothing left worth keeping a record of."""
    delete_job(db, job_id)


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


def queue_eta_seconds(db: Session) -> float | None:
    """How long until the queue drains, for the dashboard.

    Running jobs contribute their own live ETA; queued jobs contribute the
    median duration of recent completed jobs (a queued job has no progress of
    its own to extrapolate from). With one worker these run in series, so the
    estimate is the sum rather than the max.
    """
    running = list(
        db.scalars(
            select(ExtractionJob).where(ExtractionJob.status == JobStatus.processing)
        )
    )
    queued = db.scalar(
        select(func.count(ExtractionJob.id)).where(ExtractionJob.status == JobStatus.queued)
    ) or 0

    if not running and not queued:
        return None

    total = 0.0
    unknown = False
    for job in running:
        eta = job.eta_seconds
        if eta is None:
            unknown = True
        else:
            total += eta

    if queued:
        recent = list(
            db.scalars(
                select(ExtractionJob)
                .where(
                    ExtractionJob.status == JobStatus.completed,
                    ExtractionJob.started_at.is_not(None),
                    ExtractionJob.completed_at.is_not(None),
                )
                .order_by(ExtractionJob.completed_at.desc())
                .limit(10)
            )
        )
        durations = sorted(
            d for d in (j.duration_seconds for j in recent) if d and d > 0
        )
        if durations:
            median = durations[len(durations) // 2]
            total += median * queued
        else:
            unknown = True

    if total <= 0:
        return None
    # A partially-unknown estimate is still useful, but say so by rounding up
    # rather than pretending to precision we don't have.
    return round(total, 1) if not unknown else round(total * 1.25, 1)


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


# --------------------------------------------------------------------------
# KPI persistence (kpi_snapshots)
# --------------------------------------------------------------------------

# A dashboard open in a browser polls /api/kpis every few seconds. Writing a
# snapshot per poll would put tens of thousands of near-identical rows a day
# into MySQL and add an INSERT to a hot read path. Snapshots are therefore
# throttled: a capture is skipped if the newest row is younger than this.
KPI_SNAPSHOT_MIN_INTERVAL_SECONDS = 300


def latest_kpi_snapshot(db: Session) -> KpiSnapshot | None:
    return db.scalars(
        select(KpiSnapshot).order_by(KpiSnapshot.captured_at.desc()).limit(1)
    ).first()


def record_kpi_snapshot(
    db: Session,
    *,
    totals: dict,
    by_company: list[dict],
    volume: list[dict],
    queue_eta_seconds: float | None = None,
    source: str = "api",
    force: bool = False,
) -> KpiSnapshot | None:
    """Persist one KPI capture. Returns None when throttled.

    `force=True` bypasses the interval - used by the worker when a job
    reaches a terminal state, because that transition is exactly the moment
    worth having a data point for, however recently the last one was taken.
    """
    import json

    if not force:
        last = latest_kpi_snapshot(db)
        if last is not None and last.captured_at is not None:
            age = (utcnow() - last.captured_at).total_seconds()
            if age < KPI_SNAPSHOT_MIN_INTERVAL_SECONDS:
                return None

    search_counts = _search_kpi_counts(db)

    snap = KpiSnapshot(
        id=str(uuid.uuid4()),
        captured_at=utcnow(),
        source=source,
        total_jobs=int(totals.get("total_jobs", 0)),
        total_records=int(totals.get("total_records", 0)),
        total_storage_bytes=int(totals.get("total_storage_bytes", 0)),
        success_rate=float(totals.get("success_rate", 0.0)),
        completed_jobs=int(totals.get("completed_jobs", 0)),
        failed_jobs=int(totals.get("failed_jobs", 0)),
        processing_jobs=int(totals.get("processing_jobs", 0)),
        queued_jobs=int(totals.get("queued_jobs", 0)),
        queue_eta_seconds=queue_eta_seconds,
        total_searches=search_counts["total_searches"],
        completed_searches=search_counts["completed_searches"],
        total_matches=search_counts["total_matches"],
        by_company_json=json.dumps(by_company, separators=(",", ":")),
        volume_json=json.dumps(volume, separators=(",", ":")),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def capture_kpi_snapshot(
    db: Session, *, source: str = "api", force: bool = False
) -> KpiSnapshot | None:
    """Compute the current KPIs and persist them in one call.

    The convenience entry point for callers (the worker, scripts) that want a
    snapshot but have not already computed the numbers for another purpose.
    """
    return record_kpi_snapshot(
        db,
        totals=kpi_totals(db),
        by_company=kpi_by_company(db),
        volume=kpi_volume_over_time(db, days=30),
        queue_eta_seconds=queue_eta_seconds(db),
        source=source,
        force=force,
    )


def _search_kpi_counts(db: Session) -> dict:
    total_searches = db.scalar(select(func.count(ProviderSearch.id))) or 0
    completed_searches = db.scalar(
        select(func.count(ProviderSearch.id)).where(
            ProviderSearch.status == JobStatus.completed
        )
    ) or 0
    total_matches = db.scalar(
        select(func.count(SearchMatch.id)).where(SearchMatch.matched.is_(True))
    ) or 0
    return {
        "total_searches": int(total_searches),
        "completed_searches": int(completed_searches),
        "total_matches": int(total_matches),
    }


def list_kpi_snapshots(
    db: Session,
    *,
    days: int = 30,
    limit: int = 500,
    source: str | None = None,
) -> list[KpiSnapshot]:
    """Snapshots inside the trailing window, oldest first so a chart can
    plot the list as-is."""
    since = utcnow() - timedelta(days=days)
    stmt = select(KpiSnapshot).where(KpiSnapshot.captured_at >= since)
    if source:
        stmt = stmt.where(KpiSnapshot.source == source)
    # newest N inside the window, then flipped to chronological order - a
    # window with more rows than `limit` should give up its oldest, not its
    # most recent.
    rows = list(db.scalars(stmt.order_by(KpiSnapshot.captured_at.desc()).limit(limit)))
    rows.reverse()
    return rows


def prune_kpi_snapshots(db: Session, *, keep_days: int = 365) -> int:
    """Drop snapshots older than the retention window. Returns rows deleted."""
    cutoff = utcnow() - timedelta(days=keep_days)
    stale = list(db.scalars(select(KpiSnapshot).where(KpiSnapshot.captured_at < cutoff)))
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return len(stale)



# --------------------------------------------------------------------------
# Provider searches (find which in-network files hold an NPI / TIN)
# --------------------------------------------------------------------------

def create_search(
    db: Session,
    *,
    insurance_company: str,
    search_type: str,
    search_value: str,
    search_value_norm: str,
    hint: str | None = None,
    hint_strict: bool = False,
    files_to_scan: int = 0,
    target_codes: list[str] | None = None,
    code_set_name: str | None = None,
) -> ProviderSearch:
    import json

    search = ProviderSearch(
        id=str(uuid.uuid4()),
        insurance_company=insurance_company,
        search_type=SearchType(search_type),
        search_value=search_value,
        search_value_norm=search_value_norm,
        hint_strict=hint_strict,
        hint=hint,
        files_to_scan=files_to_scan,
        target_codes_json=json.dumps(target_codes) if target_codes else None,
        code_set_name=code_set_name,
        status=JobStatus.queued,
        current_stage="Queued",
    )
    db.add(search)
    db.commit()
    db.refresh(search)
    return search


def get_search(db: Session, search_id: str) -> ProviderSearch | None:
    return db.get(ProviderSearch, search_id)


def list_searches(
    db: Session,
    *,
    company: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ProviderSearch], int]:
    base = select(ProviderSearch)
    if company:
        base = base.where(ProviderSearch.insurance_company == company)
    if status:
        base = base.where(ProviderSearch.status == status)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        db.scalars(
            base.order_by(ProviderSearch.created_at.desc()).limit(limit).offset(offset)
        )
    )
    return rows, total


def claim_next_search(db: Session, worker_id: str) -> ProviderSearch | None:
    """Same atomic claim the extraction queue uses, on the search queue - see
    claim_next_job's docstring for why the SQLite path is a single atomic
    UPDATE rather than a plain SELECT-then-UPDATE (two worker processes were
    found running the identical search concurrently under the naive version:
    the corrupted, doubled-up progress log is what surfaced this bug)."""
    if settings.is_sqlite:
        candidate_id = db.scalars(
            select(ProviderSearch.id)
            .where(ProviderSearch.status == JobStatus.queued)
            .order_by(ProviderSearch.created_at.asc())
            .limit(1)
        ).first()
        if candidate_id is None:
            db.rollback()
            return None

        result = db.execute(
            update(ProviderSearch)
            .where(ProviderSearch.id == candidate_id, ProviderSearch.status == JobStatus.queued)
            .values(status=JobStatus.processing, worker_id=worker_id, current_stage="Resolving payer index")
        )
        db.commit()
        if result.rowcount == 0:
            return None  # another worker's claim landed first

        search = db.get(ProviderSearch, candidate_id)
        if search.started_at is None:
            search.started_at = utcnow()
            db.commit()
            db.refresh(search)
        return search

    stmt = (
        select(ProviderSearch)
        .where(ProviderSearch.status == JobStatus.queued)
        .order_by(ProviderSearch.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    search = db.scalars(stmt).first()
    if search is None:
        db.rollback()
        return None

    search.status = JobStatus.processing
    search.worker_id = worker_id
    search.started_at = search.started_at or utcnow()
    search.current_stage = "Resolving payer index"
    db.commit()
    db.refresh(search)
    return search


def update_search_progress(
    db: Session,
    search_id: str,
    *,
    stage: str | None = None,
    index_url: str | None = None,
    files_listed: int | None = None,
    files_to_scan: int | None = None,
    files_scanned: int | None = None,
    files_matched: int | None = None,
    bytes_scanned: int | None = None,
    scan_fraction: float | None = None,
    eta_seconds: float | None = None,
) -> None:
    s = db.get(ProviderSearch, search_id)
    if not s:
        return
    if scan_fraction is not None:
        s.scan_fraction = scan_fraction
    if stage is not None:
        s.current_stage = stage
    if index_url is not None:
        s.index_url = index_url
    if files_listed is not None:
        s.files_listed = files_listed
    if files_to_scan is not None:
        s.files_to_scan = files_to_scan
    if files_scanned is not None:
        s.files_scanned = files_scanned
    if files_matched is not None:
        s.files_matched = files_matched
    if bytes_scanned is not None:
        s.bytes_scanned = bytes_scanned
    if eta_seconds is not None:
        s.eta_seconds_est = eta_seconds
    s.updated_at = utcnow()
    db.commit()


def complete_search(db: Session, search_id: str) -> None:
    s = db.get(ProviderSearch, search_id)
    if not s:
        return
    s.status = JobStatus.completed
    s.completed_at = utcnow()
    s.current_stage = "Completed"
    s.scan_fraction = 1.0
    s.eta_seconds_est = 0.0
    db.commit()


def fail_search(db: Session, search_id: str, error_message: str) -> None:
    s = db.get(ProviderSearch, search_id)
    if not s:
        return
    s.status = JobStatus.failed
    s.completed_at = utcnow()
    s.error_message = (error_message or "Unknown error")[:60000]
    db.commit()


def request_search_cancel(db: Session, search_id: str) -> ProviderSearch | None:
    """"Stop process" from the UI.

    A search that hasn't been claimed yet is cancelled outright - there is no
    worker to tell. A running one gets the flag set; the worker sees it on
    its next chunk and winds down, then marks the row cancelled itself.
    """
    s = db.get(ProviderSearch, search_id)
    if not s:
        return None
    if s.status == JobStatus.queued:
        s.status = JobStatus.cancelled
        s.completed_at = utcnow()
        s.current_stage = "Cancelled before it started"
        s.cancel_requested = True
    elif s.status == JobStatus.processing:
        s.cancel_requested = True
        s.current_stage = "Stopping…"
    db.commit()
    db.refresh(s)
    return s


def is_search_cancel_requested(db: Session, search_id: str) -> bool:
    s = db.get(ProviderSearch, search_id)
    return bool(s and s.cancel_requested)


def mark_search_cancelled(db: Session, search_id: str) -> None:
    """Terminal state for a search the user stopped. Progress counters are
    left exactly where they were so the UI reads "stopped at 12/40"."""
    s = db.get(ProviderSearch, search_id)
    if not s:
        return
    s.status = JobStatus.cancelled
    s.completed_at = utcnow()
    s.current_stage = "Stopped by user"
    s.eta_seconds_est = 0.0
    db.commit()


def requeue_stuck_searches(db: Session) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.search_stuck_timeout)
    stuck = list(
        db.scalars(
            select(ProviderSearch).where(
                ProviderSearch.status == JobStatus.processing,
                ProviderSearch.updated_at < cutoff,
            )
        )
    )
    for s in stuck:
        if s.retry_count >= settings.worker_max_retries:
            s.status = JobStatus.failed
            s.completed_at = utcnow()
            s.error_message = s.error_message or (
                f"Worker stopped responding (no heartbeat for "
                f">{settings.search_stuck_timeout}s); retries exhausted."
            )
        else:
            s.retry_count += 1
            s.status = JobStatus.queued
            s.worker_id = None
            s.current_stage = "Queued (requeued after stall)"
            s.files_scanned = 0
    if stuck:
        db.commit()
    return len(stuck)


def record_match(db: Session, search_id: str, **fields) -> SearchMatch:
    m = SearchMatch(id=str(uuid.uuid4()), search_id=search_id, **fields)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


# A payer republishing the exact same network under a second legal entity
# name produces files whose byte size can differ slightly month to month
# (whitespace, key ordering) even though the content is the same network -
# so "identical" is a closeness check, not an exact match.
_DUPLICATE_SIZE_REL_TOL = 0.01
_DUPLICATE_SIZE_ABS_TOL = 5 * 1024 * 1024


def find_duplicate_match(
    db: Session,
    search_id: str,
    *,
    matched_tin: str,
    file_size_bytes: int,
) -> SearchMatch | None:
    """An earlier CANONICAL match in this same search with the same TIN and
    a near-identical file size, if any - evidence this file is the same
    network filed a second time under a different legal entity, not a
    genuinely different file. Only compares against matches that are
    themselves not already flagged as a duplicate, so a chain of 3+ filings
    all point at the same original."""
    if not matched_tin or not file_size_bytes:
        return None
    candidates = db.scalars(
        select(SearchMatch).where(
            SearchMatch.search_id == search_id,
            SearchMatch.matched.is_(True),
            SearchMatch.matched_tin == matched_tin,
            SearchMatch.duplicate_of_match_id.is_(None),
        )
    )
    for c in candidates:
        other = c.file_size_bytes
        if not other:
            continue
        diff = abs(other - file_size_bytes)
        if diff <= _DUPLICATE_SIZE_ABS_TOL or diff <= _DUPLICATE_SIZE_REL_TOL * max(other, file_size_bytes):
            return c
    return None


def list_matches(
    db: Session,
    search_id: str,
    *,
    matched_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[SearchMatch], int]:
    """(rows, total). `limit=None` returns everything.

    Sorting happens in Python because the order is a composite the DB can't
    express cleanly (matched, confidence rank, size, name) and the row count
    per search is bounded by files_to_scan - a few hundred at most.
    """
    stmt = select(SearchMatch).where(SearchMatch.search_id == search_id)
    if matched_only:
        # Both the byte-scan hit AND not a same-network-refiled duplicate of
        # an earlier match - "N files contain this provider" should mean N
        # files worth downloading, not N filings of the same few networks.
        stmt = stmt.where(
            SearchMatch.matched.is_(True),
            SearchMatch.duplicate_of_match_id.is_(None),
        )
    # matches first, then the strongest evidence, then the cheapest to process
    rows = list(db.scalars(stmt))
    rows.sort(
        key=lambda m: (
            not m.matched,
            0 if m.confidence == "strong" else 1,
            m.file_size_bytes or 1 << 62,
            m.filename,
        )
    )
    total = len(rows)
    if limit is not None:
        rows = rows[offset : offset + limit]
    return rows, total


def get_match(db: Session, match_id: str) -> SearchMatch | None:
    return db.get(SearchMatch, match_id)


def attach_job_to_match(db: Session, match_id: str, job_id: str) -> None:
    m = db.get(SearchMatch, match_id)
    if not m:
        return
    m.job_id = job_id
    db.commit()


def save_match_cpt_check(
    db: Session,
    match_id: str,
    *,
    checked_codes: list[str],
    matched_codes: list[str],
    items_scanned: int,
    bytes_scanned: int,
    exhausted: bool,
    budget_exceeded: bool,
    error: str | None,
) -> SearchMatch | None:
    """Persist a cpt_probe.scan_billing_codes() result onto the match it was
    run for, so the search results page doesn't re-scan a 300MB file just
    because it was re-opened."""
    import json

    m = db.get(SearchMatch, match_id)
    if not m:
        return None
    m.cpt_checked_at = utcnow()
    m.cpt_checked_codes_json = json.dumps(sorted(checked_codes))
    m.cpt_matched_codes_json = json.dumps(matched_codes)
    m.cpt_items_scanned = items_scanned
    m.cpt_bytes_scanned = bytes_scanned
    m.cpt_exhausted = exhausted
    m.cpt_budget_exceeded = budget_exceeded
    m.cpt_error = error or None
    db.commit()
    db.refresh(m)
    return m


def save_match_discovered_codes(
    db: Session,
    match_id: str,
    *,
    discovered_codes: list[str],
    error: str | None,
) -> SearchMatch | None:
    """Persist a target_cpt_probe.scan_target_billing_codes(codes=None)
    discovery result (Provider Lookup page) - same target_billed_codes_json
    column the Search page's automatic search-time verification writes, just
    filled on demand here instead."""
    import json

    m = db.get(SearchMatch, match_id)
    if not m:
        return None
    m.target_codes_discovered_at = utcnow()
    m.target_billed_codes_json = json.dumps(discovered_codes) if discovered_codes else None
    m.target_codes_discovery_error = error or None
    db.commit()
    db.refresh(m)
    return m


def jobs_for_search(db: Session, search_id: str) -> list[ExtractionJob]:
    return list(
        db.scalars(
            select(ExtractionJob)
            .where(ExtractionJob.search_id == search_id)
            .order_by(ExtractionJob.created_at.asc())
        )
    )


# --------------------------------------------------------------------------
# Saved CPT/HCPCS code sets
# --------------------------------------------------------------------------

def create_code_set(db: Session, *, name: str, codes: list[str]) -> CptCodeSet:
    import json

    cs = CptCodeSet(id=str(uuid.uuid4()), name=name, codes_json=json.dumps(codes))
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return cs


def list_code_sets(db: Session) -> list[CptCodeSet]:
    return list(db.scalars(select(CptCodeSet).order_by(func.lower(CptCodeSet.name))))


def get_code_set(db: Session, set_id: str) -> CptCodeSet | None:
    return db.get(CptCodeSet, set_id)


def get_code_set_by_name(db: Session, name: str) -> CptCodeSet | None:
    """Case-insensitive - "E&M codes" and "e&m codes" are the same set."""
    return db.scalars(
        select(CptCodeSet).where(func.lower(CptCodeSet.name) == name.strip().lower())
    ).first()


def update_code_set(
    db: Session,
    set_id: str,
    *,
    name: str | None = None,
    codes: list[str] | None = None,
) -> CptCodeSet | None:
    import json

    cs = db.get(CptCodeSet, set_id)
    if not cs:
        return None
    if name is not None:
        cs.name = name
    if codes is not None:
        cs.codes_json = json.dumps(codes)
    cs.updated_at = utcnow()
    db.commit()
    db.refresh(cs)
    return cs


def delete_code_set(db: Session, set_id: str) -> bool:
    cs = db.get(CptCodeSet, set_id)
    if not cs:
        return False
    db.delete(cs)
    db.commit()
    return True
