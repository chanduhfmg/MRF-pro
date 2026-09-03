"""Pydantic request/response models for the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .models import ExtractionJob, JobStatus


class CompanyOut(BaseModel):
    key: str
    label: str


class UploadInitIn(BaseModel):
    insurance_company: str
    filename: str
    file_size: int


class UploadInitOut(BaseModel):
    upload_id: str
    chunk_size: int
    received_chunks: list[int] = []


class UploadFinishIn(BaseModel):
    total_chunks: int


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    insurance_company: str
    insurance_company_label: str
    source_filename: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    total_bytes: int | None
    bytes_processed: int
    records_processed: int
    current_stage: str | None
    output_size_bytes: int | None
    record_count: int | None
    output_file_count: int | None
    error_message: str | None
    percent_complete: float
    eta_seconds: float | None
    duration_seconds: float | None
    has_output: bool

    @classmethod
    def from_job(cls, job: ExtractionJob) -> "JobOut":
        from .constants import company_label

        return cls(
            id=job.id,
            insurance_company=job.insurance_company,
            insurance_company_label=company_label(job.insurance_company),
            source_filename=job.source_filename,
            status=job.status,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            total_bytes=job.total_bytes,
            bytes_processed=job.bytes_processed,
            records_processed=job.records_processed,
            current_stage=job.current_stage,
            output_size_bytes=job.output_size_bytes,
            record_count=job.record_count,
            output_file_count=job.output_file_count,
            error_message=job.error_message,
            percent_complete=job.percent_complete,
            eta_seconds=job.eta_seconds,
            duration_seconds=job.duration_seconds,
            has_output=bool(job.output_zip_path),
        )


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int
    limit: int
    offset: int


class JobStatusOut(BaseModel):
    """Lightweight payload for the 3-4s poll."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: JobStatus
    percent: float
    eta_seconds: float | None
    stage: str | None
    records_processed: int
    bytes_processed: int
    total_bytes: int | None
    error_message: str | None

    @classmethod
    def from_job(cls, job: ExtractionJob) -> "JobStatusOut":
        return cls(
            id=job.id,
            status=job.status,
            percent=job.percent_complete,
            eta_seconds=job.eta_seconds,
            stage=job.current_stage,
            records_processed=job.records_processed,
            bytes_processed=job.bytes_processed,
            total_bytes=job.total_bytes,
            error_message=job.error_message,
        )


# --- KPI dashboard ---------------------------------------------------------

class KpiTotals(BaseModel):
    total_jobs: int
    total_records: int
    total_storage_bytes: int
    success_rate: float          # 0..1 over finished (completed+failed) jobs
    completed_jobs: int
    failed_jobs: int
    processing_jobs: int
    queued_jobs: int


class CompanyCount(BaseModel):
    company: str
    company_label: str
    jobs: int
    records: int


class VolumePoint(BaseModel):
    date: str                    # YYYY-MM-DD
    jobs: int
    records: int


class KpiOut(BaseModel):
    totals: KpiTotals
    by_company: list[CompanyCount]
    volume_over_time: list[VolumePoint]
    recent_jobs: list[JobOut]
