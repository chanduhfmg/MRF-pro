"""
ORM model for the one table this app has. Mirrors the spec DDL, with a few
operational columns added (documented inline) so the polling worker can
claim jobs and recover ones that die mid-run.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .utils import utcnow


class JobStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    insurance_company: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_file_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=20),
        default=JobStatus.queued,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    # heartbeat: bumped on every progress write. Used to detect dead jobs.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    records_processed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(100), nullable=True)

    output_zip_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # final extracted row count (records_processed is the *live* counter;
    # this is frozen at completion, matches spec feature 4 "record_count").
    record_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- worker bookkeeping (not in the spec DDL, kept minimal) ---
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---- convenience -------------------------------------------------------

    @property
    def percent_complete(self) -> float:
        if not self.total_bytes:
            return 100.0 if self.status == JobStatus.completed else 0.0
        pct = 100.0 * (self.bytes_processed or 0) / self.total_bytes
        return max(0.0, min(100.0, round(pct, 1)))

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.completed_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def eta_seconds(self) -> float | None:
        """elapsed / bytes_processed * bytes_remaining  (spec formula)."""
        if self.status != JobStatus.processing:
            return None
        if not self.total_bytes or not self.bytes_processed or not self.started_at:
            return None
        elapsed = (utcnow() - self.started_at).total_seconds()
        if elapsed <= 0:
            return None
        remaining = max(0, self.total_bytes - self.bytes_processed)
        rate = self.bytes_processed / elapsed
        if rate <= 0:
            return None
        return round(remaining / rate, 1)
