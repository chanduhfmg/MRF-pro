"""
Durable local store of each payer's MRF index (table-of-contents) URL.

WHY THIS EXISTS
---------------
Resolving a payer's index means HEAD-probing a fan-out of candidate URLs -
for Aetna and MVP, dozens of them. Doing that at the start of every search
costs seconds to minutes, hammers the payer, and makes the whole feature stop
working the moment the network hiccups. Payers only re-publish monthly, so the
answer is stable for weeks.

So: resolve once, write the answer here, and read it back for the next month.
A search does zero network work to find its index.

    backend/payer_index.json
    {
      "anthem": {
        "url": "https://…/2026-09-01_anthem_index.json.gz",
        "resolved_at": 1789...,          # unix seconds
        "expires_at": null,              # for signed URLs, see below
        "size_bytes": 10650000000,
        "source": "resolver",            # resolver | pinned | seed | manual
        "reporting_month": "2026-09"
      }, ...
    }

SIGNED URLS
-----------
Cigna's index is a CloudFront URL carrying `Expires=<epoch>` in its query
string; AWS presigned URLs carry `X-Amz-Date` + `X-Amz-Expires`. Caching one
of those for a month would hand the worker a URL that 403s. `expires_at` is
parsed out of the URL and the entry is treated as stale well before it lapses,
regardless of the monthly schedule.

FRESHNESS
---------
An entry is stale when any of these is true:
  * older than INDEX_REFRESH_DAYS (default 30)
  * past, or within INDEX_EXPIRY_MARGIN of, its signed expiry
  * its reporting month is behind the current month by more than a month
Refresh is best-effort: if the network is down, a stale-but-present URL is
still returned (with `stale: true`) rather than failing the search outright.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import BACKEND_ROOT, settings

log = logging.getLogger("mrf.index_store")

STORE_FILE = BACKEND_ROOT / "payer_index.json"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Seed: index URLs verified working on 2026-09-14.
#
# These make a fresh checkout useful immediately and give the store something
# to fall back on when the network is unavailable. They are a starting point,
# not gospel - the first successful refresh replaces them.
# ---------------------------------------------------------------------------
SEED: dict[str, dict] = {
    "uhc": {
        "url": "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/",
        "size_bytes": None,
        "reporting_month": None,   # the blob API always serves the current period
        "note": "blob-listing API; not dated, so it does not go stale monthly",
    },
    "anthem": {
        "url": "https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com"
               "/anthem/2026-09-01_anthem_index.json.gz",
        "size_bytes": 10_650_000_000,
        "reporting_month": "2026-09",
    },
}


# A payer table of contents is a listing of every in-network file; the
# smallest real one we have seen is ~21 MB. Anything this small is an error
# page, a captive-portal interception, or a redirect stub - never an index.
MIN_PLAUSIBLE_INDEX_BYTES = 64 * 1024


@dataclass
class IndexEntry:
    payer: str
    url: str
    resolved_at: float
    source: str = "resolver"
    size_bytes: int | None = None
    expires_at: float | None = None
    reporting_month: str | None = None
    note: str | None = None
    # Did this URL actually serve JSON/gzip when we stored it? An entry that
    # was never confirmed is re-checked rather than trusted for a month.
    verified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Signed-URL expiry
# ---------------------------------------------------------------------------

def signed_expiry(url: str) -> float | None:
    """Unix seconds at which a signed URL stops working, or None if it is a
    plain URL. Handles CloudFront (`Expires`) and AWS SigV4 presigning
    (`X-Amz-Date` + `X-Amz-Expires`)."""
    try:
        q = parse_qs(urlsplit(url).query)
    except Exception:
        return None

    exp = q.get("Expires") or q.get("expires")
    if exp and exp[0].isdigit():
        return float(exp[0])

    amz_date, amz_exp = q.get("X-Amz-Date"), q.get("X-Amz-Expires")
    if amz_date and amz_exp and amz_exp[0].isdigit():
        try:
            t = datetime.strptime(amz_date[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return t.timestamp() + int(amz_exp[0])
        except Exception:
            return None
    return None


_MONTH_RE = re.compile(r"(20\d{2})[-_/](\d{2})")


def reporting_month_of(url: str) -> str | None:
    """'…/reporting_month=2026-09/2026-09-01_x_index.json' -> '2026-09'."""
    m = _MONTH_RE.search(url)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_delta(a: str, b: str) -> int:
    """Whole months from a to b; both 'YYYY-MM'."""
    try:
        ay, am = (int(x) for x in a.split("-"))
        by, bm = (int(x) for x in b.split("-"))
        return (by - ay) * 12 + (bm - am)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _read_raw() -> dict:
    if not STORE_FILE.exists():
        return {}
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("could not read %s; starting empty", STORE_FILE)
        return {}


def _write_raw(data: dict) -> None:
    try:
        tmp = STORE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(STORE_FILE)
    except Exception:
        log.exception("could not write %s", STORE_FILE)


def _ensure_seeded(data: dict) -> dict:
    """Fill in any payer that has no entry at all from SEED."""
    changed = False
    for payer, s in SEED.items():
        if payer not in data:
            data[payer] = IndexEntry(
                payer=payer, url=s["url"], resolved_at=0.0, source="seed",
                size_bytes=s.get("size_bytes"),
                expires_at=signed_expiry(s["url"]),
                reporting_month=s.get("reporting_month"),
                note=s.get("note"),
            ).to_dict()
            changed = True
    if changed:
        _write_raw(data)
    return data


def load() -> dict[str, dict]:
    with _lock:
        return _ensure_seeded(_read_raw())


def get(payer: str) -> IndexEntry | None:
    raw = load().get(payer)
    if not raw:
        return None
    try:
        return IndexEntry(**{**raw, "payer": payer})
    except Exception:
        return None


def put(
    payer: str,
    url: str,
    *,
    source: str = "resolver",
    size_bytes: int | None = None,
    note: str | None = None,
    verified: bool = False,
) -> IndexEntry:
    entry = IndexEntry(
        payer=payer,
        url=url,
        resolved_at=time.time(),
        source=source,
        size_bytes=size_bytes,
        expires_at=signed_expiry(url),
        reporting_month=reporting_month_of(url),
        note=note,
        verified=verified,
    )
    with _lock:
        data = _ensure_seeded(_read_raw())
        data[payer] = entry.to_dict()
        _write_raw(data)
    log.info("stored index URL for %s (source=%s)", payer, source)
    return entry


def forget(payer: str) -> None:
    with _lock:
        data = _read_raw()
        if data.pop(payer, None) is not None:
            _write_raw(data)


def invalidate(payer: str, why: str) -> None:
    """Mark a stored URL as not-to-be-trusted after it failed in real use.

    A URL that HEADs fine but cannot actually be parsed as an index is worse
    than no URL: it fails every search until it ages out. When the search
    pipeline hits that, it calls this and the next attempt re-resolves.
    """
    with _lock:
        data = _ensure_seeded(_read_raw())
        entry = data.get(payer)
        if not entry:
            return
        entry["verified"] = False
        entry["resolved_at"] = 0.0
        entry["note"] = f"invalidated: {why}"[:300]
        _write_raw(data)
    log.warning("invalidated stored index for %s: %s", payer, why)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def staleness(entry: IndexEntry | None, now: float | None = None) -> str | None:
    """None when the entry is good to use; otherwise why it needs refreshing."""
    if entry is None or not entry.url:
        return "missing"
    now = now or time.time()

    if entry.expires_at is not None:
        if now >= entry.expires_at - settings.index_expiry_margin:
            return "signature expiring"

    if entry.source == "pinned":
        return None      # a human pinned it; never expire it on a schedule

    # Never trust an unconfirmed entry for a month. A transient network fault
    # (captive portal, proxy interception) can make a dead URL look resolvable
    # for a moment; without this it would be cached as good until next month.
    if entry.source == "resolver" and not entry.verified:
        return "never verified"
    if (
        entry.size_bytes is not None
        and entry.size_bytes < MIN_PLAUSIBLE_INDEX_BYTES
        and entry.source != "seed"
    ):
        return f"implausibly small for an index ({entry.size_bytes} bytes)"

    age_days = (now - (entry.resolved_at or 0)) / 86400.0
    if age_days >= settings.index_refresh_days:
        return f"older than {settings.index_refresh_days} days"

    if entry.reporting_month:
        behind = _month_delta(entry.reporting_month, current_month())
        if behind > 1:
            return f"reporting month {entry.reporting_month} is {behind} months behind"

    return None


def is_fresh(entry: IndexEntry | None) -> bool:
    return staleness(entry) is None


# ---------------------------------------------------------------------------
# The entry point the rest of the app uses
# ---------------------------------------------------------------------------

def get_index_url(payer: str, *, allow_network: bool = True, force: bool = False) -> dict:
    """The payer's index URL, from the local store wherever possible.

    Returns {url, from_store, stale, reason, entry, error}. A fresh entry
    costs zero network calls. A stale one is refreshed if `allow_network`;
    if that refresh fails the stale URL is still handed back with
    `stale: True` - a month-old URL that might work beats no search at all.
    """
    entry = get(payer)
    reason = staleness(entry)

    if entry and reason is None and not force:
        return {"url": entry.url, "from_store": True, "stale": False,
                "reason": None, "entry": entry, "error": None}

    if not allow_network:
        return {"url": entry.url if entry else None, "from_store": bool(entry),
                "stale": True, "reason": reason, "entry": entry,
                "error": None if entry else "no stored index URL"}

    # refresh
    from .payers import resolve_index_url

    try:
        r = resolve_index_url(payer, force=True)
    except Exception as e:  # noqa: BLE001
        r = {"url": None, "error": f"{type(e).__name__}: {e}"}

    if r.get("url"):
        # Confirm it really serves an index before committing it for a month.
        # resolve_index_url already sniffed it, but a blip between the two
        # calls is exactly how junk gets cached as good.
        from .payers import sniff_json

        ok, why = sniff_json(r["url"])
        size = r.get("size_bytes")
        too_small = size is not None and size < MIN_PLAUSIBLE_INDEX_BYTES
        if ok and not too_small:
            fresh = put(payer, r["url"], source="resolver",
                        size_bytes=size, verified=True)
            return {"url": fresh.url, "from_store": False, "stale": False,
                    "reason": reason, "entry": fresh, "error": None}
        r = {
            "url": None,
            "error": (
                f"resolved {r['url']} but it did not verify"
                f" ({why or f'only {size} bytes'}) - not storing it"
            ),
        }
        log.warning("index for %s failed verification: %s", payer, r["error"])

    # refresh failed - fall back to whatever we had
    if entry and entry.url:
        log.warning(
            "index refresh for %s failed (%s); using stored URL from %s",
            payer, r.get("error"),
            datetime.fromtimestamp(entry.resolved_at or 0, timezone.utc).date()
            if entry.resolved_at else "seed",
        )
        return {"url": entry.url, "from_store": True, "stale": True,
                "reason": reason, "entry": entry, "error": r.get("error")}

    return {"url": None, "from_store": False, "stale": True, "reason": reason,
            "entry": None, "error": r.get("error") or "could not resolve an index URL"}


def refresh_all(*, force: bool = False, only_stale: bool = True) -> list[dict]:
    """Refresh every payer. Used by the startup sweep and the manual endpoint."""
    from .payers import PAYER_SOURCES

    out: list[dict] = []
    for payer in PAYER_SOURCES:
        entry = get(payer)
        reason = staleness(entry)
        if only_stale and not force and reason is None:
            out.append({"payer": payer, "action": "kept", "url": entry.url if entry else None,
                        "reason": None})
            continue
        r = get_index_url(payer, allow_network=True, force=True)
        out.append({
            "payer": payer,
            "action": "refreshed" if r["url"] and not r["stale"] else "failed",
            "url": r["url"],
            "reason": reason,
            "error": r["error"],
        })
    return out


def refresh_in_background() -> None:
    """Fire-and-forget refresh of stale entries, so API startup never blocks
    on the network and the first search of the month is already warm."""
    def _run() -> None:
        try:
            for r in refresh_all(only_stale=True):
                if r["action"] == "refreshed":
                    log.info("index refreshed: %s -> %s", r["payer"], r["url"])
                elif r["action"] == "failed":
                    log.warning("index refresh failed for %s: %s", r["payer"], r["error"])
        except Exception:
            log.exception("background index refresh crashed")

    threading.Thread(target=_run, name="index-refresh", daemon=True).start()
