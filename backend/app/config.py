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
    npi_lookup_threads: int = Field(default=8, alias="NPI_LOOKUP_THREADS")
    npi_max_per_row: int = Field(default=5, alias="NPI_MAX_PER_ROW")

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
