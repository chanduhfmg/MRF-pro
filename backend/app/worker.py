"""
Redis-free background worker.

Polls the extraction_jobs table for the oldest `queued` row, atomically
claims it (status -> processing), runs the pipeline, and writes the result
back. Runs as its OWN process, next to uvicorn, on every host:

    python -m app.worker

Multiple instances are safe: MySQL uses SELECT ... FOR UPDATE SKIP LOCKED
so two workers never grab the same job. On every idle loop it also requeues
jobs whose heartbeat has gone stale (a worker that crashed mid-run), so a
job is never left silently stuck in "processing".
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import traceback

from .config import settings
from .database import init_db, session_scope
from . import crud
from .pipeline.context import JobCancelled
from .pipeline.run import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("mrf.worker")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = False


def _handle_signal(signum, _frame):  # noqa: ANN001
    global _stop
    log.info("received signal %s, finishing current job then exiting", signum)
    _stop = True


def process_one_search() -> bool:
    """Claim + run a single provider search. Returns True if one ran.

    Searches are polled before extractions: a search is the interactive half
    of the product (someone is watching the spinner), while an extraction is
    a long background grind, so making a user wait behind a 3-hour extraction
    for a 2-minute search would be the wrong trade.
    """
    from .pipeline.search import SearchReporter, run_search

    with session_scope() as db:
        search = crud.claim_next_search(db, WORKER_ID)
        if search is None:
            return False
        search_id = search.id
        log.info(
            "claimed search %s | company=%s %s=%s",
            search_id, search.insurance_company,
            search.search_type, search.search_value,
        )

    try:
        with session_scope() as db:
            search = crud.get_search(db, search_id)
        reporter = SearchReporter(search_id)
        summary, was_cancelled = run_search(search, reporter)
        with session_scope() as db:
            crud.update_search_progress(
                db,
                search_id,
                index_url=summary["index_url"],
                files_listed=summary["files_listed"],
                files_scanned=summary["files_scanned"],
                files_matched=summary["files_matched"],
                bytes_scanned=summary["bytes_scanned"],
            )
            if was_cancelled:
                crud.mark_search_cancelled(db, search_id)
            else:
                crud.complete_search(db, search_id)
        log.info(
            "search %s %s (%s/%s files matched)",
            search_id, "STOPPED BY USER" if was_cancelled else "COMPLETED",
            summary["files_matched"], summary["files_scanned"],
        )
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded
        tb = traceback.format_exc()
        log.error("search %s FAILED: %s\n%s", search_id, exc, tb)
        with session_scope() as db:
            crud.fail_search(db, search_id, f"{type(exc).__name__}: {exc}\n\n{tb}")

    # A run reaching a terminal state is the moment the KPIs actually moved,
    # so it gets a snapshot regardless of how recently one was taken. Never
    # let bookkeeping turn a finished run into a crashed worker.
    _snapshot_kpis()
    return True


def process_one() -> bool:
    """Claim + run a single job. Returns True if a job was processed."""
    with session_scope() as db:
        job = crud.claim_next_job(db, WORKER_ID)
        if job is None:
            return False
        job_id = job.id
        log.info(
            "claimed job %s | company=%s file=%s bytes=%s",
            job_id, job.insurance_company, job.source_filename, job.total_bytes,
        )

    try:
        # fetch a detached-but-fresh copy for the pipeline to read from
        with session_scope() as db:
            job = crud.get_job(db, job_id)
        result = run_pipeline(job)
        with session_scope() as db:
            crud.mark_completed(
                db,
                job_id,
                output_zip_path=str(result.zip_path),
                output_size_bytes=result.zip_size_bytes,
                record_count=result.record_count,
                output_file_count=result.output_file_count,
                output_xlsx_path=str(result.xlsx_path) if result.xlsx_path else None,
                requested_code_count=result.requested_code_count,
                matched_code_count=result.matched_code_count,
                matched_codes=result.matched_codes,
            )
        log.info("job %s COMPLETED (%s rows)", job_id, result.record_count)
    except JobCancelled:
        # The user asked for it: not a failure. Drop whatever was half-built
        # so a cancelled job never offers a misleading partial zip.
        log.info("job %s STOPPED BY USER", job_id)
        _discard_partial_output(job)
        with session_scope() as db:
            crud.mark_job_cancelled(db, job_id)
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded
        tb = traceback.format_exc()
        log.error("job %s FAILED: %s\n%s", job_id, exc, tb)
        with session_scope() as db:
            crud.mark_failed(db, job_id, f"{type(exc).__name__}: {exc}\n\n{tb}")

    # A run reaching a terminal state is the moment the KPIs actually moved,
    # so it gets a snapshot regardless of how recently one was taken. Never
    # let bookkeeping turn a finished run into a crashed worker.
    _snapshot_kpis()
    return True


def _snapshot_kpis() -> None:
    """Persist a KPI capture to `kpi_snapshots`."""
    try:
        with session_scope() as db:
            crud.capture_kpi_snapshot(db, source="worker", force=True)
    except Exception:  # noqa: BLE001
        log.warning("KPI snapshot not recorded", exc_info=True)


def _discard_partial_output(job) -> None:
    """Remove partial CSVs and, for a payer-sourced job, the half- or fully-
    downloaded MRF (multi-GB scratch we'd otherwise keep for nothing)."""
    import shutil
    from pathlib import Path

    from .storage import output_dir_for

    try:
        shutil.rmtree(output_dir_for(job.id), ignore_errors=True)
    except Exception:
        log.warning("could not remove output dir for job %s", job.id)
    if getattr(job, "source_url", None) and not settings.keep_downloaded_mrf:
        src = Path(job.source_file_path)
        for p in (src, src.with_suffix(src.suffix + ".part")):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                log.warning("could not remove %s", p)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    init_db()
    log.info(
        "worker %s up | mode=%s | db=%s | poll=%.1fs",
        WORKER_ID,
        settings.extraction_mode,
        "sqlite" if settings.is_sqlite else "mysql",
        settings.worker_poll_interval,
    )

    last_sweep = 0.0
    while not _stop:
        # periodic stuck-job sweep (every ~30s)
        now = time.time()
        if now - last_sweep > 30:
            try:
                with session_scope() as db:
                    n = crud.requeue_stuck_jobs(db)
                    n += crud.requeue_stuck_searches(db)
                if n:
                    log.warning("requeued/failed %d stuck item(s)", n)
            except Exception:
                log.exception("stuck-job sweep failed")
            last_sweep = now

        try:
            # searches first - see process_one_search's docstring
            did_work = process_one_search() or process_one()
        except Exception:
            log.exception("unexpected error in worker loop")
            did_work = False

        if not did_work and not _stop:
            time.sleep(settings.worker_poll_interval)

    log.info("worker %s stopped", WORKER_ID)


if __name__ == "__main__":
    main()
