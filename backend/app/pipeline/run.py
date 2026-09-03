"""Dispatch a claimed job to the stub or real pipeline."""
from __future__ import annotations

import logging

from ..config import settings
from ..storage import output_dir_for
from .context import ProgressReporter
from .result import PipelineResult

log = logging.getLogger("mrf.worker.run")


def run_pipeline(job) -> PipelineResult:
    out_dir = output_dir_for(job.id)
    reporter = ProgressReporter(job_id=job.id, total_bytes=job.total_bytes)

    if settings.extraction_mode == "stub":
        from .stub import run_stub

        log.info("job %s -> STUB pipeline", job.id)
        return run_stub(job, reporter, out_dir)

    from .real import run_real

    log.info("job %s -> REAL pipeline", job.id)
    return run_real(job, reporter, out_dir)
