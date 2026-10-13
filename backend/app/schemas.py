"""Pydantic request/response models for the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import ExtractionJob, JobStatus, KpiSnapshot, SearchType


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
    # payer-sourced jobs (created from an NPI/TIN search)
    source_url: str | None = None
    search_id: str | None = None
    target_type: str | None = None
    target_value: str | None = None
    filter_to_target: bool = False
    downloaded_bytes: int = 0
    throughput_bps: float | None = None
    # None = the built-in default code set
    target_codes: list[str] | None = None
    cancel_requested: bool = False
    plan_name: str | None = None
    code_set_name: str | None = None
    # the single-workbook download exists (output fit in one Excel sheet)
    has_xlsx: bool = False
    # what the browser will name the download, e.g. UHC_Oxford-Metro-27_NPI…_20260914_default.xlsx
    download_name: str | None = None
    # How many of the requested CPT/HCPCS codes were actually found in this
    # file, out of how many were requested, and which ones. Set once the job
    # completes; None before then. A 0 record_count with matched_code_count
    # also 0 means the codes never appeared in the file at all - with
    # matched_code_count > 0 it means they appeared, just not billed by the
    # searched provider.
    requested_code_count: int | None = None
    matched_code_count: int | None = None
    matched_codes: list[str] | None = None

    @classmethod
    def from_job(cls, job: ExtractionJob) -> "JobOut":
        import json

        from .constants import company_label
        from .naming import download_basename

        codes: list[str] | None = None
        if getattr(job, "target_codes", None):
            try:
                parsed = json.loads(job.target_codes)
                codes = [str(c) for c in parsed] if isinstance(parsed, list) else None
            except Exception:
                codes = None

        has_xlsx = bool(getattr(job, "output_xlsx_path", None))
        return cls(
            target_codes=codes,
            cancel_requested=bool(getattr(job, "cancel_requested", False)),
            plan_name=getattr(job, "plan_name", None),
            code_set_name=getattr(job, "code_set_name", None),
            has_xlsx=has_xlsx,
            download_name=(
                download_basename(job, "xlsx" if has_xlsx else "zip")
                if job.output_zip_path
                else None
            ),
            source_url=job.source_url,
            search_id=job.search_id,
            target_type=job.target_type,
            target_value=job.target_value,
            filter_to_target=job.filter_to_target,
            downloaded_bytes=job.downloaded_bytes or 0,
            throughput_bps=job.throughput_bps,
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
            requested_code_count=getattr(job, "requested_code_count", None),
            matched_code_count=getattr(job, "matched_code_count", None),
            matched_codes=job.matched_codes or None,
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
    downloaded_bytes: int = 0
    throughput_bps: float | None = None
    cancel_requested: bool = False

    @classmethod
    def from_job(cls, job: ExtractionJob) -> "JobStatusOut":
        return cls(
            cancel_requested=bool(getattr(job, "cancel_requested", False)),
            id=job.id,
            status=job.status,
            percent=job.percent_complete,
            eta_seconds=job.eta_seconds,
            stage=job.current_stage,
            records_processed=job.records_processed,
            bytes_processed=job.bytes_processed,
            total_bytes=job.total_bytes,
            error_message=job.error_message,
            downloaded_bytes=job.downloaded_bytes or 0,
            throughput_bps=job.throughput_bps,
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
    # Estimated seconds until every running + queued job is done.
    queue_eta_seconds: float | None = None


class KpiSnapshotOut(BaseModel):
    """One persisted KPI capture from `kpi_snapshots`."""

    id: str
    captured_at: datetime
    source: str

    totals: KpiTotals
    queue_eta_seconds: float | None = None

    total_searches: int = 0
    completed_searches: int = 0
    total_matches: int = 0

    by_company: list[CompanyCount] = []
    volume_over_time: list[VolumePoint] = []

    @classmethod
    def from_snapshot(cls, s: "KpiSnapshot") -> "KpiSnapshotOut":
        return cls(
            id=s.id,
            captured_at=s.captured_at,
            source=s.source,
            totals=KpiTotals(
                total_jobs=s.total_jobs,
                total_records=s.total_records,
                total_storage_bytes=s.total_storage_bytes,
                success_rate=s.success_rate,
                completed_jobs=s.completed_jobs,
                failed_jobs=s.failed_jobs,
                processing_jobs=s.processing_jobs,
                queued_jobs=s.queued_jobs,
            ),
            queue_eta_seconds=s.queue_eta_seconds,
            total_searches=s.total_searches,
            completed_searches=s.completed_searches,
            total_matches=s.total_matches,
            by_company=[CompanyCount(**c) for c in s.by_company],
            volume_over_time=[VolumePoint(**v) for v in s.volume_over_time],
        )


# --- Payer MRF sources ------------------------------------------------------

class PayerTried(BaseModel):
    url: str
    ok: bool
    error: str | None = None
    size_bytes: int | None = None


class PayerSourceOut(BaseModel):
    """A payer's hardcoded MRF index location and whether it is live."""

    key: str
    label: str
    publisher_url: str
    verified: str
    notes: str
    index_url: str | None = None
    size_bytes: int | None = None
    resolved: bool = False
    from_cache: bool = False
    # a URL was hand-pinned via payer_sources.json / POST .../index
    pinned: bool = False
    # the host refused the TLS handshake (firewall) - not a URL problem
    blocked: bool = False
    blocked_host: str | None = None
    error: str | None = None
    tried: list[PayerTried] = []
    # --- local store (backend/payer_index.json): what a search actually uses
    stored_url: str | None = None
    stored_at: float | None = None          # unix seconds
    stored_source: str | None = None        # resolver | pinned | seed | manual
    stored_stale: bool | None = None
    stored_stale_reason: str | None = None
    reporting_month: str | None = None


class PinIndexIn(BaseModel):
    index_url: str


# --- Provider (NPI/TIN) search ---------------------------------------------

class SearchCreateIn(BaseModel):
    insurance_company: str
    search_type: SearchType
    search_value: str
    # Free-text nudge ("New York", a plan name) used to order which files get
    # scanned first - or, with hint_strict, to exclude everything else.
    hint: str | None = None
    # False (default): hint only reorders. True: only files whose
    # description/plan_name/filename contain `hint` are listed at all -
    # "only the HMO plans", not "HMO plans scanned first".
    hint_strict: bool = False
    max_files: int | None = Field(
        default=None, ge=1,
        description="How many in-network files to byte-scan. The user chooses "
                    "the limit; higher values take longer.",
    )
    # Required: a file only becomes a match if the target TIN/NPI bills at
    # least one of these here, resolved through provider groups - not merely
    # "the code is in the file somewhere". Explicit codes win over
    # code_set_id, same convention as ExtractSelectionIn.
    codes: list[str] = Field(default_factory=list)
    code_set_id: str | None = None


class SearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    insurance_company: str
    insurance_company_label: str
    search_type: SearchType
    search_value: str
    hint: str | None
    hint_strict: bool = False
    status: JobStatus
    current_stage: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    index_url: str | None
    files_listed: int
    files_to_scan: int
    files_scanned: int
    files_matched: int
    bytes_scanned: int
    percent_complete: float
    eta_seconds: float | None
    duration_seconds: float | None
    error_message: str | None
    cancel_requested: bool = False
    # The codes chosen before this search ran - what gated every match.
    target_codes: list[str] = Field(default_factory=list)
    code_set_name: str | None = None

    @classmethod
    def from_search(cls, s) -> "SearchOut":
        from .constants import company_label

        return cls(
            cancel_requested=bool(s.cancel_requested),
            id=s.id,
            insurance_company=s.insurance_company,
            insurance_company_label=company_label(s.insurance_company),
            search_type=s.search_type,
            search_value=s.search_value,
            hint=s.hint,
            hint_strict=bool(s.hint_strict),
            status=s.status,
            current_stage=s.current_stage,
            created_at=s.created_at,
            started_at=s.started_at,
            completed_at=s.completed_at,
            index_url=s.index_url,
            files_listed=s.files_listed,
            files_to_scan=s.files_to_scan,
            files_scanned=s.files_scanned,
            files_matched=s.files_matched,
            bytes_scanned=s.bytes_scanned,
            percent_complete=s.percent_complete,
            eta_seconds=s.eta_seconds,
            duration_seconds=s.duration_seconds,
            error_message=s.error_message,
            target_codes=s.target_codes,
            code_set_name=s.code_set_name,
        )


class SearchListOut(BaseModel):
    searches: list[SearchOut]
    total: int
    limit: int
    offset: int


class SearchStatusOut(BaseModel):
    """Lightweight payload for the search page's poll."""

    id: str
    status: JobStatus
    percent: float
    eta_seconds: float | None
    stage: str | None
    files_listed: int
    files_to_scan: int
    files_scanned: int
    files_matched: int
    bytes_scanned: int
    error_message: str | None
    cancel_requested: bool = False

    @classmethod
    def from_search(cls, s) -> "SearchStatusOut":
        return cls(
            cancel_requested=bool(s.cancel_requested),
            id=s.id,
            status=s.status,
            percent=s.percent_complete,
            eta_seconds=s.eta_seconds,
            stage=s.current_stage,
            files_listed=s.files_listed,
            files_to_scan=s.files_to_scan,
            files_scanned=s.files_scanned,
            files_matched=s.files_matched,
            bytes_scanned=s.bytes_scanned,
            error_message=s.error_message,
        )


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    search_id: str
    location: str
    filename: str
    description: str | None
    plan_name: str | None
    plan_id: str | None
    plan_market_type: str | None
    reference_count: int
    file_size_bytes: int | None
    matched: bool
    confidence: str | None
    provider_group_id: str | None
    matched_tin: str | None
    npi_count: int
    context: str | None
    bytes_scanned: int
    scan_seconds: float | None
    exhausted: bool
    budget_exceeded: bool
    error: str | None
    job_id: str | None
    # Set when this is the same network as an earlier match in this search,
    # republished under a second legal entity (same TIN, near-identical
    # size) - hidden from matched_only=true by default. Points at the
    # earlier, canonical match's id.
    duplicate_of_match_id: str | None = None
    # Rough seconds to extract+enrich this file, from its size. Lets the user
    # see the cost of a selection before committing to it.
    estimated_seconds: float | None = None
    # Which of the search's own target_codes (SearchOut.target_codes) the
    # target TIN/NPI was confirmed to bill in this file, resolved through
    # provider groups at search time - this is WHY the file matched at all,
    # so it's always populated for a matched row, no extra click needed.
    #
    # For a codeless search (Provider Lookup page), this instead holds the
    # on-demand "Discover billed codes" result once run;
    # target_codes_discovered_at distinguishes "never run" (null) from
    # "ran and found nothing" (set, with an empty list here).
    target_billed_codes: list[str] | None = None
    target_codes_discovered_at: datetime | None = None
    target_codes_discovery_error: str | None = None

    # --- cached CPT/HCPCS coverage (cpt_probe.scan_billing_codes) ---------
    # None until someone checks this file. `cpt_checked_codes` records which
    # codes produced `cpt_matched_codes`, so the frontend can tell a cached
    # answer apart from a stale one (the user picked different codes since).
    cpt_checked_at: datetime | None = None
    cpt_checked_codes: list[str] | None = None
    cpt_matched_codes: list[str] | None = None
    cpt_items_scanned: int | None = None
    cpt_exhausted: bool | None = None
    cpt_budget_exceeded: bool | None = None
    cpt_error: str | None = None

    @classmethod
    def from_match(cls, m) -> "MatchOut":  # noqa: ANN001
        return cls(
            id=m.id,
            search_id=m.search_id,
            location=m.location,
            filename=m.filename,
            description=m.description,
            plan_name=m.plan_name,
            plan_id=m.plan_id,
            plan_market_type=m.plan_market_type,
            reference_count=m.reference_count,
            file_size_bytes=m.file_size_bytes,
            matched=m.matched,
            confidence=m.confidence,
            provider_group_id=m.provider_group_id,
            matched_tin=m.matched_tin,
            npi_count=m.npi_count,
            context=m.context,
            bytes_scanned=m.bytes_scanned,
            scan_seconds=m.scan_seconds,
            exhausted=m.exhausted,
            budget_exceeded=m.budget_exceeded,
            error=m.error,
            job_id=m.job_id,
            duplicate_of_match_id=m.duplicate_of_match_id,
            cpt_checked_at=m.cpt_checked_at,
            cpt_checked_codes=m.cpt_checked_codes or None,
            cpt_matched_codes=m.cpt_matched_codes if m.cpt_checked_at else None,
            cpt_items_scanned=m.cpt_items_scanned,
            cpt_exhausted=m.cpt_exhausted,
            cpt_budget_exceeded=m.cpt_budget_exceeded,
            cpt_error=m.cpt_error,
            target_billed_codes=m.target_billed_codes if (m.matched or m.target_codes_discovered_at) else None,
            target_codes_discovered_at=m.target_codes_discovered_at,
            target_codes_discovery_error=m.target_codes_discovery_error,
        )


class CptCheckIn(BaseModel):
    """Which codes to check this match for. Explicit codes win over a saved
    set, same convention as ExtractSelectionIn."""

    codes: list[str] = Field(default_factory=list)
    code_set_id: str | None = None


class CptCheckOut(BaseModel):
    match_id: str
    requested_codes: list[str]
    matched_codes: list[str]
    matched_count: int
    requested_count: int
    items_scanned: int
    bytes_scanned: int
    file_size_bytes: int | None
    elapsed_seconds: float
    exhausted: bool
    budget_exceeded: bool
    error: str | None = None


class CptCheckStatusOut(BaseModel):
    """Live progress for a running (or just-finished) CPT check - polled by
    the UI so it can show real bytes-scanned-based ETA instead of a static
    upfront guess, and so a running check can be told to stop."""

    match_id: str
    status: str  # "running" | "done" | "error" | "cancelled"
    phase: str | None = None  # "download" | "lookup" | "scan", while running
    requested_codes: list[str]
    matched_codes: list[str]
    items_scanned: int
    bytes_scanned: int
    file_size_bytes: int | None = None
    elapsed_seconds: float
    eta_seconds: float | None = None
    exhausted: bool | None = None
    budget_exceeded: bool | None = None
    error: str | None = None


class DiscoverCodesStatusOut(BaseModel):
    """Live progress for a "Discover billed codes" run (Provider Lookup page)
    - full discovery, no fixed code list: `discovered_codes` is every code
    the search's target TIN/NPI bills in this file, found so far."""

    match_id: str
    status: str  # "running" | "done" | "error" | "cancelled"
    phase: str | None = None  # "download" | "lookup" | "scan", while running
    discovered_codes: list[str]
    items_scanned: int
    file_size_bytes: int | None = None
    elapsed_seconds: float
    eta_seconds: float | None = None
    exhausted: bool | None = None
    budget_exceeded: bool | None = None
    error: str | None = None


class MatchListOut(BaseModel):
    search: SearchOut
    # One page of results (see total/limit/offset). matched_count and
    # scanned_count describe the whole search, not the page.
    matches: list[MatchOut]
    total: int
    limit: int
    offset: int
    matched_count: int
    scanned_count: int
    # Total estimated wall-clock if every matched file were extracted.
    estimated_total_seconds: float | None = None


class ExtractSelectionIn(BaseModel):
    """Turn chosen search results into extraction jobs."""

    match_ids: list[str] = Field(
        default_factory=list,
        description="Explicit matches to extract. Ignored when top_n is set.",
    )
    top_n: int | None = Field(
        default=None, ge=1, le=200,
        description="Instead of listing ids, take the N best matches.",
    )
    filter_to_target: bool = Field(
        default=True,
        description="Keep only rows for the searched NPI/TIN. False extracts "
                    "every provider group in the file.",
    )
    # Which CPT/HCPCS codes to pull. Explicit `codes` win; otherwise a saved
    # set by id; otherwise the script's built-in E&M + ophthalmic default.
    codes: list[str] | None = Field(
        default=None,
        description="Explicit CPT/HCPCS codes to extract. Replaces the default set.",
    )
    code_set_id: str | None = Field(
        default=None, description="Use a saved code set instead of listing codes."
    )


# --- Saved CPT/HCPCS code sets ----------------------------------------------

class CodeSetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    codes: list[str] = Field(..., min_length=1)


class CodeSetUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    codes: list[str] | None = Field(default=None, min_length=1)


class CodeSetOut(BaseModel):
    id: str
    name: str
    codes: list[str]
    code_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, cs) -> "CodeSetOut":
        codes = cs.codes
        return cls(
            id=cs.id,
            name=cs.name,
            codes=codes,
            code_count=len(codes),
            created_at=cs.created_at,
            updated_at=cs.updated_at,
        )


class ExtractSelectionOut(BaseModel):
    search_id: str
    created: int
    jobs: list[JobOut]
    estimated_total_seconds: float | None = None
