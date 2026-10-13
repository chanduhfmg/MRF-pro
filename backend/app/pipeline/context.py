"""
Progress reporting for a running job.

The pipeline stages call `reporter.update(...)` as often as they like; the
reporter throttles actual DB writes to at most once per `min_interval`
seconds (a stage change or an explicit flush always writes through). Each
write is its own short transaction so we never hold a multi-hour DB lock.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..database import session_scope
from .. import crud

log = logging.getLogger("mrf.worker.progress")


class JobCancelled(Exception):
    """Raised inside the pipeline when the job row is no longer ours."""


@dataclass
class ProgressReporter:
    job_id: str
    total_bytes: int | None = None
    min_interval: float = 2.0

    _last_write: float = field(default=0.0, init=False)
    _stage: str | None = field(default=None, init=False)
    _bytes: int = field(default=0, init=False)
    _records: int = field(default=0, init=False)
    _downloaded: int | None = field(default=None, init=False)
    _eta: float | None = field(default=None, init=False)
    _bps: float | None = field(default=None, init=False)

    def set_stage(self, stage: str) -> None:
        self._stage = stage
        self._write(force=True)

    def update(
        self,
        *,
        stage: str | None = None,
        bytes_processed: int | None = None,
        records_processed: int | None = None,
        total_bytes: int | None = None,
        downloaded_bytes: int | None = None,
        eta_seconds: float | None = None,
        throughput_bps: float | None = None,
    ) -> None:
        if stage is not None:
            self._stage = stage
        if bytes_processed is not None:
            self._bytes = bytes_processed
        if records_processed is not None:
            self._records = records_processed
        if total_bytes is not None:
            self.total_bytes = total_bytes
        if downloaded_bytes is not None:
            self._downloaded = downloaded_bytes
        if eta_seconds is not None:
            self._eta = eta_seconds
        if throughput_bps is not None:
            self._bps = throughput_bps
        self._write(force=stage is not None)

    def flush(self) -> None:
        self._write(force=True)

    def check_cancel(self) -> None:
        """Abort the pipeline if the user has asked for this job to stop.

        Called on every actual progress write (so at most every
        `min_interval` seconds, plus every stage change) and available to
        the pipeline at cheap points between phases. Raises JobCancelled,
        which unwinds the generator chain - CSV writers and the download's
        .part file clean themselves up on the way out.
        """
        try:
            with session_scope() as db:
                cancelled = crud.is_job_cancel_requested(db, self.job_id)
        except Exception:
            log.exception("cancel check failed for job %s", self.job_id)
            return
        if cancelled:
            raise JobCancelled(f"job {self.job_id} stopped by user")

    # -- internal ---------------------------------------------------------

    def _write(self, *, force: bool) -> None:
        now = time.time()
        if not force and (now - self._last_write) < self.min_interval:
            return
        self._last_write = now
        try:
            with session_scope() as db:
                crud.update_progress(
                    db,
                    self.job_id,
                    stage=self._stage,
                    bytes_processed=self._bytes,
                    records_processed=self._records,
                    total_bytes=self.total_bytes,
                    downloaded_bytes=self._downloaded,
                    eta_seconds=self._eta,
                    throughput_bps=self._bps,
                )
        except Exception:  # progress writes must never kill a job
            log.exception("progress write failed for job %s", self.job_id)
        # Deliberately outside the try: JobCancelled must propagate.
        self.check_cancel()
