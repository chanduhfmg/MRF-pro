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
    ) -> None:
        if stage is not None:
            self._stage = stage
        if bytes_processed is not None:
            self._bytes = bytes_processed
        if records_processed is not None:
            self._records = records_processed
        if total_bytes is not None:
            self.total_bytes = total_bytes
        self._write(force=stage is not None)

    def flush(self) -> None:
        self._write(force=True)

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
                )
        except Exception:  # progress writes must never kill a job
            log.exception("progress write failed for job %s", self.job_id)
