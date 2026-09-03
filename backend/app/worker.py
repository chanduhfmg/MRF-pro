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
            )
        log.info("job %s COMPLETED (%s rows)", job_id, result.record_count)
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded
        tb = traceback.format_exc()
        log.error("job %s FAILED: %s\n%s", job_id, exc, tb)
        with session_scope() as db:
            crud.mark_failed(db, job_id, f"{type(exc).__name__}: {exc}\n\n{tb}")
    return True


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
                if n:
                    log.warning("requeued/failed %d stuck job(s)", n)
            except Exception:
                log.exception("stuck-job sweep failed")
            last_sweep = now

        try:
            did_work = process_one()
        except Exception:
            log.exception("unexpected error in worker loop")
            did_work = False

        if not did_work and not _stop:
            time.sleep(settings.worker_poll_interval)

    log.info("worker %s stopped", WORKER_ID)


if __name__ == "__main__":
    main()
