"""
ORM model for the one table this app has. Mirrors the spec DDL, with a few
operational columns added (documented inline) so the polling worker can
claim jobs and recover ones that die mid-run.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .utils import utcnow


class JobStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    # Stopped on purpose by the user. Distinct from `failed` because nothing
    # went wrong - a search stopped early still has every match it found.
    cancelled = "cancelled"


class SearchType(str, enum.Enum):
    npi = "npi"
    tin = "tin"


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
    # A single .xlsx of the whole output, written when it fits in a sheet
    # (Excel caps at 1,048,576 rows). NULL above that - the CSV zip remains.
    output_xlsx_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Carried for the download filename: the payer's plan name (from the
    # search match) and which saved code set was used, if any.
    plan_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    code_set_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # final extracted row count (records_processed is the *live* counter;
    # this is frozen at completion, matches spec feature 4 "record_count").
    record_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- CPT/HCPCS code coverage --------------------------------------------
    # A file can legitimately contain the searched NPI/TIN and still yield
    # zero rows, if none of the requested codes happen to appear in it. That
    # is silent and confusing without a number to point at, so the worker
    # records how many of the requested codes it actually found (out of how
    # many were requested) and the codes themselves - independent of
    # record_count, which can be 0 for a perfectly correct, fully-matched-set
    # extraction if the matched codes just carry no rows for this provider.
    requested_code_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_code_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON array of the billing codes (from the requested set) actually seen
    # in the file's in_network items, sorted.
    matched_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- worker bookkeeping (not in the spec DDL, kept minimal) ---
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- payer-sourced jobs (search -> extract flow) ------------------------
    # When set, the worker downloads this URL instead of expecting an upload.
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Which provider search produced this job, and which matched file it is.
    search_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    match_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The NPI/TIN the user searched for, carried through so extraction can
    # narrow output to that provider.
    target_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    filter_to_target: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # JSON array of CPT/HCPCS codes this job extracts. NULL means the full
    # default set baked into extract_tic_all.py (E&M + ophthalmic).
    target_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set by the API when the user clicks "Stop extraction". The worker's
    # progress reporter sees it on its next write and aborts the pipeline;
    # partial output is discarded and the row ends as `cancelled`.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- ETA ----------------------------------------------------------------
    # Written by the worker's RateEstimator each heartbeat. Storing the
    # estimate (rather than recomputing it from elapsed/bytes on read) means
    # the API reports the same smoothed number the worker actually observed,
    # and queue time never inflates it.
    eta_seconds_est: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_bps: Mapped[float | None] = mapped_column(Float, nullable=True)

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
        """Seconds remaining. Prefers the worker's smoothed estimate; falls
        back to the plain elapsed/bytes formula before the first heartbeat."""
        if self.status != JobStatus.processing:
            return None
        if self.eta_seconds_est is not None and self.eta_seconds_est >= 0:
            return round(self.eta_seconds_est, 1)
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

    @property
    def matched_codes(self) -> list[str]:
        """Which requested billing codes were actually found in the file,
        sorted. Empty (not None) once the job has run, even if nothing
        matched - that is itself the answer."""
        import json

        try:
            data = json.loads(self.matched_codes_json or "[]")
        except Exception:
            return []
        return [str(c) for c in data] if isinstance(data, list) else []


class ProviderSearch(Base):
    """One 'find the files containing this NPI/TIN' run.

    Modelled as a queued job rather than a request handler because scanning a
    payer's in-network files is minutes-to-hours of network work - the same
    reason extraction is a job. It gets the same claim/heartbeat/requeue
    treatment, so a dead worker never strands a search in `processing`.
    """

    __tablename__ = "provider_searches"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    insurance_company: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    search_type: Mapped[SearchType] = mapped_column(
        SAEnum(SearchType, native_enum=False, length=10), nullable=False
    )
    search_value: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # digits-only, leading zeros stripped - what the probe actually looks for
    search_value_norm: Mapped[str] = mapped_column(String(40), nullable=False)
    hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # False (default): hint only REORDERS candidates (matching files scanned
    # first, nothing excluded). True: hint FILTERS - only files whose
    # description/plan_name/filename contain it are even listed as
    # candidates, e.g. "only the HMO plans, not every plan scanned with HMO
    # ones first."
    hint_strict: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The codes chosen before this search ran. Required at creation: the
    # search filters to files where the target actually bills at least one
    # of these, so there is no meaningful codeless search to represent.
    target_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_set_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=20),
        default=JobStatus.queued,
        nullable=False,
        index=True,
    )
    current_stage: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    index_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    files_listed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_to_scan: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_scanned: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Fractional scan progress in [0,1], counting partly-read files.
    # files_scanned alone only moves when a whole file finishes, and with
    # several large files probed in parallel that means a bar pinned at 0%
    # until everything lands at once. This is what the percentage actually
    # renders from.
    scan_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)

    eta_seconds_est: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set by the API when the user clicks "Stop process". The worker polls it
    # between chunks and winds the scan down; the row then moves to
    # `cancelled`, keeping every match already recorded.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def target_codes(self) -> list[str]:
        return _json_list(self.target_codes_json)

    @property
    def percent_complete(self) -> float:
        if self.status == JobStatus.completed:
            return 100.0
        # A cancelled search freezes wherever it got to - it did not finish,
        # so it must not claim 100%.
        if not self.files_to_scan:
            # still resolving the index / listing files
            return 2.0 if self.status == JobStatus.processing else 0.0
        frac = (
            self.scan_fraction
            if self.scan_fraction is not None
            else self.files_scanned / self.files_to_scan
        )
        return max(0.0, min(100.0, round(100.0 * frac, 1)))

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.completed_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def eta_seconds(self) -> float | None:
        if self.status != JobStatus.processing:
            return None
        if self.eta_seconds_est is not None and self.eta_seconds_est >= 0:
            return round(self.eta_seconds_est, 1)
        if not self.files_to_scan or not self.files_scanned or not self.started_at:
            return None
        elapsed = (utcnow() - self.started_at).total_seconds()
        if elapsed <= 0:
            return None
        rate = self.files_scanned / elapsed
        if rate <= 0:
            return None
        return round((self.files_to_scan - self.files_scanned) / rate, 1)


class SearchMatch(Base):
    """One in-network file examined during a search.

    Non-matching files are kept too (matched=False): seeing "scanned 40, 3 hit"
    is the difference between a trustworthy result and a shrug, and it lets a
    re-run skip what was already ruled out.
    """

    __tablename__ = "search_matches"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    search_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    location: Mapped[str] = mapped_column(String(2000), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    plan_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    plan_market_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_group_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    matched_tin: Mapped[str | None] = mapped_column(String(40), nullable=True)
    npi_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when this match's TIN and file size are near-identical to an
    # earlier match in the same search - the same network republished under
    # a second legal entity (payers routinely file one plan's rates twice
    # under two entity names) rather than a genuinely different file. Points
    # at the earlier (canonical) match's id. matched_only=true hides these so
    # "N files contain this provider" means N files worth actually
    # downloading, not N filings of the same handful of networks.
    duplicate_of_match_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Which of the search's own target_codes this file's target TIN/NPI was
    # confirmed to bill (resolved through provider groups, not just "the code
    # appears somewhere in the file" - see target_cpt_probe.py). A file only
    # gets `matched=True` when this is non-empty, so every visible match
    # already answers "yes, and here's which codes" with no extra click.
    #
    # Also doubles as the Provider Lookup page's "Discover billed codes"
    # result: that flow's searches carry no target_codes (nothing to verify
    # at search time), so this stays null until the user clicks discover -
    # `target_codes_discovered_at` distinguishes "never run" from "search
    # already answered it".
    target_billed_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_codes_discovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_codes_discovery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    bytes_scanned: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    scan_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    budget_exceeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # set once the user turns this match into an extraction job
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # --- CPT/HCPCS coverage check (cpt_probe.scan_billing_codes) -----------
    # Separate from the NPI/TIN probe above and from extraction: this answers
    # "of the codes I've selected, how many actually appear in this file?",
    # on demand, before committing to a multi-minute extraction. Cached so
    # reloading the search results doesn't re-scan; cpt_checked_codes_json
    # records exactly which codes produced this result, so the UI can tell a
    # fresh answer from a stale one when the user changes their selection.
    cpt_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cpt_checked_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpt_matched_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpt_items_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpt_bytes_scanned: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpt_exhausted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cpt_budget_exceeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cpt_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def cpt_checked_codes(self) -> list[str]:
        return _json_list(self.cpt_checked_codes_json)

    @property
    def cpt_matched_codes(self) -> list[str]:
        return _json_list(self.cpt_matched_codes_json)

    @property
    def target_billed_codes(self) -> list[str]:
        return _json_list(self.target_billed_codes_json)


def _json_list(raw: str | None) -> list[str]:
    import json

    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return [str(c) for c in data] if isinstance(data, list) else []


class CptCodeSet(Base):
    """A named, reusable list of CPT/HCPCS codes ("E&M codes", "Cardiology",
    "Ophthalmology"). Picked from a dropdown when starting an extraction so
    nobody has to retype 80 codes, and so two people extracting "cardiology"
    get the same definition of it."""

    __tablename__ = "cpt_code_sets"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # JSON array of strings, upper-cased and de-duplicated on write.
    codes_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def codes(self) -> list[str]:
        import json

        try:
            data = json.loads(self.codes_json or "[]")
        except Exception:
            return []
        return [str(c) for c in data] if isinstance(data, list) else []


class KpiSnapshot(Base):
    """A point-in-time capture of the dashboard's KPIs, persisted to MySQL.

    The KPI numbers themselves are derived - every one of them can be
    recomputed from `extraction_jobs` as it stands *right now*. What cannot
    be recomputed is what they looked like last Tuesday: jobs get pruned,
    rows get deleted, and `success_rate` is a ratio over whatever survives.
    Writing a row here each time the dashboard is read turns those live
    aggregates into a history you can trend, without changing how the live
    numbers are produced.

    Deliberately denormalised (the per-company and per-day breakdowns are
    stored as JSON text rather than child tables): a snapshot is written and
    read whole, never joined against, and keeping it one row means capturing
    it costs a single INSERT on the request path.
    """

    __tablename__ = "kpi_snapshots"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    # What caused the capture: 'api' (dashboard read), 'worker' (a job
    # reached a terminal state) or 'manual' (script / operator).
    source: Mapped[str] = mapped_column(String(20), default="api", nullable=False)

    # --- totals (mirrors schemas.KpiTotals) ---
    total_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_records: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completed_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queued_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queue_eta_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- provider-search side of the house -------------------------------
    # Not part of the live /api/kpis payload, but the cheap half of "what was
    # this system doing on the 3rd?" - recorded while we are writing anyway.
    total_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_matches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- breakdowns, stored as JSON text ---
    # [{company, company_label, jobs, records}, ...]
    by_company_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{date, jobs, records}, ...] for the trailing window
    volume_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def by_company(self) -> list[dict]:
        return _load_json_list(self.by_company_json)

    @property
    def volume_over_time(self) -> list[dict]:
        return _load_json_list(self.volume_json)


def _load_json_list(raw: str | None) -> list[dict]:
    import json

    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
