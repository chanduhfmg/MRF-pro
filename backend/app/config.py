"""
Central configuration. Everything comes from environment variables (or a
`.env` file next to this backend folder) so the identical codebase runs on
the local Windows dev box and on the deploy server at 172.22.6.98 with only
the `.env` differing.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = two levels up from this file (backend/app/config.py -> repo/)
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---
    database_url: str = Field(default="", alias="DATABASE_URL")

    # --- Storage ---
    data_root: str = Field(default="./data", alias="DATA_ROOT")

    # --- HTTP ---
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="CORS_ORIGINS"
    )
    frontend_dist: str = Field(default="", alias="FRONTEND_DIST")

    # --- Upload ---
    upload_chunk_size: int = Field(default=8 * 1024 * 1024, alias="UPLOAD_CHUNK_SIZE")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")

    # --- Worker ---
    extraction_mode: str = Field(default="real", alias="EXTRACTION_MODE")
    worker_poll_interval: float = Field(default=3.0, alias="WORKER_POLL_INTERVAL")
    worker_stuck_timeout: int = Field(default=900, alias="WORKER_STUCK_TIMEOUT")
    worker_max_retries: int = Field(default=2, alias="WORKER_MAX_RETRIES")
    csv_row_threshold: int = Field(default=500_000, alias="CSV_ROW_THRESHOLD")
    # Hard ceiling on parts per job, regardless of row_threshold. Once hit,
    # the writer stops opening new files and keeps appending to the last one
    # instead - a job never produces more than this many CSV parts, which
    # keeps zip entry counts (and total files an analyst has to deal with)
    # bounded even for the largest full-network extracts.
    csv_max_parts: int = Field(default=4000, alias="CSV_MAX_PARTS")
    npi_lookup_threads: int = Field(default=8, alias="NPI_LOOKUP_THREADS")
    # 0 = look up EVERY NPI on every row (the default). The standalone script
    # capped this at 5 to bound runtime, which silently left most providers'
    # ZIPs blank on large groups. Unique NPIs are cached, so a provider costs
    # one NPPES call however many rows it appears on.
    npi_max_per_row: int = Field(default=0, alias="NPI_MAX_PER_ROW")

    # --- Payer index resolution ---
    # Payers re-publish monthly, so a resolved index URL is stored locally
    # (backend/payer_index.json) and reused for this many days. A search then
    # does ZERO network work to find its index.
    index_refresh_days: int = Field(default=30, alias="INDEX_REFRESH_DAYS")
    # Refresh a signed URL this long before its signature lapses (seconds).
    index_expiry_margin: int = Field(default=6 * 3600, alias="INDEX_EXPIRY_MARGIN")
    # Refresh stale entries in the background when the API starts.
    index_refresh_on_startup: bool = Field(default=True, alias="INDEX_REFRESH_ON_STARTUP")
    # In-process memo for a single resolve() pass (seconds). Not the store.
    index_cache_ttl: int = Field(default=6 * 3600, alias="INDEX_CACHE_TTL")
    # Max candidate URLs HEAD-probed per payer while resolving.
    index_probe_limit: int = Field(default=12, alias="INDEX_PROBE_LIMIT")
    index_probe_timeout: float = Field(default=12.0, alias="INDEX_PROBE_TIMEOUT")

    # --- Provider (NPI/TIN) search ---
    # Ceiling on in-network files enumerated from a payer TOC. A payer can
    # publish thousands (UHC: ~6,900) to millions (Anthem) of entries; ranking
    # only needs a bounded head of that list to pick good candidates from, and
    # listing the whole thing just to rank the top `files_to_scan` of it is
    # wasted time on the "Reading table of contents" stage for no benefit.
    # 0 disables the ceiling (list everything) for the rare case that's needed.
    search_max_index_entries: int = Field(
        default=200, ge=0, alias="SEARCH_MAX_INDEX_ENTRIES"
    )
    # Default number of files actually byte-scanned per search (UI can lower it).
    search_max_files: int = Field(default=40, alias="SEARCH_MAX_FILES")
    search_concurrency: int = Field(default=6, alias="SEARCH_CONCURRENCY")
    # Per-file budgets. A probe stops at whichever comes first.
    #
    # search_probe_max_bytes does double duty: it's also the cutoff
    # mrf_index.rank_entries() uses to decide which candidates are "fully
    # scannable" (Tier 0 - prioritized, biggest first, since a bigger file is
    # a broader network) versus "over budget" (Tier 2 - deprioritized, only
    # partially conclusive). Shrinking it doesn't just make an individual
    # probe give up sooner - it shrinks the pool of files the ranking
    # considers valuable, pushing genuinely large (likely-to-matter) files
    # out of Tier 0 and toward small/stub files instead. Kept generous for
    # that reason; search_probe_timeout is the safe lever for "give up on a
    # slow file faster" - it only bounds one probe's runtime and has no
    # effect on which files get chosen.
    search_probe_max_bytes: int = Field(
        default=400 * 1024 * 1024, alias="SEARCH_PROBE_MAX_BYTES"
    )
    search_probe_timeout: float = Field(default=90.0, alias="SEARCH_PROBE_TIMEOUT")
    search_stuck_timeout: int = Field(default=1800, alias="SEARCH_STUCK_TIMEOUT")

    # --- Download (payer-sourced extraction jobs) ---
    download_timeout: float = Field(default=180.0, alias="DOWNLOAD_TIMEOUT")
    # Keep a downloaded MRF after the job finishes? Off by default - these are
    # multi-GB and the extracted CSVs are what anyone actually wants.
    keep_downloaded_mrf: bool = Field(default=False, alias="KEEP_DOWNLOADED_MRF")

    @field_validator("extraction_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = (v or "real").strip().lower()
        if v not in {"stub", "real"}:
            raise ValueError("EXTRACTION_MODE must be 'stub' or 'real'")
        return v

    # ---- Derived helpers ----------------------------------------------------

    @property
    def data_root_path(self) -> Path:
        p = Path(self.data_root)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        return p

    @property
    def raw_dir(self) -> Path:
        return self.data_root_path / "raw"

    @property
    def output_dir(self) -> Path:
        return self.data_root_path / "output"

    @property
    def uploads_tmp_dir(self) -> Path:
        return self.data_root_path / "_uploads"

    @property
    def effective_database_url(self) -> str:
        """A blank DATABASE_URL means 'local dev' -> use a SQLite file so the
        app runs with zero infra. MySQL is used the moment a URL is set."""
        if self.database_url.strip():
            return self.database_url.strip()
        sqlite_path = (BACKEND_ROOT / "mrf_dashboard.db").as_posix()
        return f"sqlite:///{sqlite_path}"

    @property
    def is_sqlite(self) -> bool:
        return self.effective_database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.output_dir, self.uploads_tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
