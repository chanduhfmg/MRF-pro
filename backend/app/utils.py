"""Small shared helpers."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC 'now'. The DB columns are naive DATETIME, so we keep
    Python-side timestamps naive-UTC too (datetime.utcnow() is deprecated
    on 3.12+ and removed later)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
