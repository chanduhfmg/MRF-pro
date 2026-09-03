"""
Create tables (works for SQLite or MySQL, whatever DATABASE_URL points at).

    cd backend
    python -m scripts.init_db
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.database import engine, init_db  # noqa: E402


def main() -> None:
    print(f"database: {'sqlite' if settings.is_sqlite else 'mysql'} -> {engine.url}")
    init_db()
    print("tables ready (extraction_jobs).")


if __name__ == "__main__":
    main()
