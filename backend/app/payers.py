"""
Hardcoded MRF source registry — where each payer's Table-of-Contents ("index")
lives, and the resolver that turns that knowledge into a URL that is live
right now.

Three ways a payer publishes its index, each with its own resolver:

  templates   Date-templated static URLs (Anthem's S3 bucket, UHC's blob API).
              Expanded over recent months and HEAD-probed newest-first.

  lookup      A small JSON the payer's own site serves that *contains* the
              current index URL (Cigna: /static/mrf/latest.json -> a signed
              CloudFront link). Fetched fresh each time; signed links expire.

  healthsparq Aetna and MVP outsource publishing to HealthSparq/Kyruus:
                mrf.healthsparq.com/{tenant}-egress.nophi.kyruushsq.com/prd/mrf/
                  {INSURER}/{BRAND}/{date}/tableOfContents/{date}_{Entity}_index.json.gz
              with one index per legal entity per posting date. Confirmed
              from published examples - but see BLOCKED below.

BLOCKED HOSTS
-------------
mrf.healthsparq.com sits behind an Imperva WAF that refuses the TLS handshake
(`TLSV1_ALERT_ACCESS_DENIED`) to automated clients - every client we tried,
from every network we tried. That is a server-side policy, not a URL problem.
The resolver detects it, stops probing that host after the first refusal, and
reports `blocked=True` so the UI can say what is actually wrong instead of
"no candidate responded".

OVERRIDES (no code change)
--------------------------
`backend/payer_sources.json` is merged over this registry at import time:

    { "aetna": { "index_url": "https://.../2026-09-05_..._index.json.gz" } }

`index_url` pins an exact URL and skips resolution. `POST /api/payers/{key}/index`
writes this file for you, after verifying the URL serves JSON.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .config import BACKEND_ROOT, settings

log = logging.getLogger("mrf.payers")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 MRF-Dashboard/1.0"
)


@dataclass(frozen=True)
class PayerSource:
    key: str
    label: str
    publisher_url: str
    # "templates" | "lookup" | "healthsparq"
    resolver: str = "templates"
    # templates: ordered URL templates with {ymd} {Y} {m} {d}
    index_templates: tuple[str, ...] = ()
    # lookup: the JSON that names the current index
    lookup_url: str | None = None
    # healthsparq: path components
    hsq_tenant: str = ""
    hsq_insurer: str = ""
    hsq_brands: tuple[str, ...] = ()
    hsq_entities: tuple[str, ...] = ()
    # Which reader understands the index once we have it:
    #   "cms_index" - standard TiC table of contents (reporting_structure[])
    #   "uhc_blobs" - UHC's blob-listing API
    toc_kind: str = "cms_index"
    verified: str = "best-effort"
    notes: str = ""
    # Absolute URL that bypasses resolution (set via overrides).
    index_url: str | None = None
    months_back: int = 3
    # Day-of-month offsets to try (payers post on the 1st; Aetna on the 5th).
    day_offsets: tuple[int, ...] = (0, 1, 4)
    # Seconds a successful resolution stays cached.
    cache_ttl: int | None = None


# ---------------------------------------------------------------------------
# The registry. Order here is the order the UI dropdown shows.
# ---------------------------------------------------------------------------

PAYER_SOURCES: dict[str, PayerSource] = {
    "uhc": PayerSource(
        key="uhc",
        label="UnitedHealthcare (UHC)",
        publisher_url="https://transparency-in-coverage.uhc.com/",
        resolver="templates",
        index_templates=("https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/",),
        toc_kind="uhc_blobs",
        verified="confirmed",
        notes=(
            "UHC has no single table of contents: its blob API lists every file "
            "for the current reporting period in one response. Sizes come back "
            "inline, so file-size estimates are exact here."
        ),
        day_offsets=(0,),
    ),
    "aetna": PayerSource(
        key="aetna",
        label="Aetna",
        publisher_url=(
            "https://health1.aetna.com/app/public/#/one/insurerCode=AETNACVS_I"
            "&brandCode=ALICSI/machine-readable-transparency-in-coverage"
        ),
        resolver="healthsparq",
        hsq_tenant="aetnacvs",
        hsq_insurer="AETNACVS_I",
        hsq_brands=("ALICFI", "ALICSI"),
        hsq_entities=(
            "Aetna-Life-Insurance-Company",
            "Aetna-Health-of-California-Inc-",
            "Aetna-Health-Inc-",
            "Aetna-Health-Insurance-Company",
        ),
        verified="pattern-confirmed",
        notes=(
            "Published through HealthSparq/Kyruus. The path pattern is confirmed "
            "from real files (Aetna posts on the 5th), but mrf.healthsparq.com's "
            "firewall refuses TLS connections from automated clients, so "
            "resolution is expected to report 'blocked' until that changes or an "
            "index URL that works from this host is pinned."
        ),
        day_offsets=(4, 5, 0, 1, 6, 3),
    ),
    "anthem": PayerSource(
        key="anthem",
        label="Anthem",
        publisher_url="https://www.anthem.com/machine-readable-file/search/",
        resolver="templates",
        index_templates=(
            "https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com/anthem/{ymd}_anthem_index.json.gz",
        ),
        verified="confirmed",
        notes="Confirmed monthly S3 pattern. The index itself is ~10 GB gzipped.",
        day_offsets=(0,),
    ),
    "cigna": PayerSource(
        key="cigna",
        label="Cigna Healthcare",
        publisher_url="https://www.cigna.com/legal/compliance/machine-readable-files",
        resolver="lookup",
        lookup_url="https://www.cigna.com/static/mrf/latest.json",
        verified="confirmed",
        notes=(
            "Cigna's page reads /static/mrf/latest.json and links the first "
            "mrfs[].files[].url - a signed CloudFront URL for the current "
            "month's table of contents. Re-read on every resolution because the "
            "signature expires."
        ),
        cache_ttl=1800,
    ),
    "mvp": PayerSource(
        key="mvp",
        label="MVP Health Care",
        publisher_url="https://www.mvphealthcare.com/developers/machine-readable-files",
        resolver="healthsparq",
        hsq_tenant="mvp",
        hsq_insurer="MVP_I",
        hsq_brands=("MVP",),
        hsq_entities=(
            "MVP-Health-Plan-Inc-",
            "MVP-Health-Insurance-Company",
            "MVP-Health-Care",
            "MVP-Health-Services-Corp-",
        ),
        verified="pattern-confirmed",
        notes=(
            "MVP's developer page links to its HealthSparq portal "
            "(mvp.healthsparq.com, insurer MVP_I, brand MVP). Same host-level "
            "TLS block as Aetna - see that entry."
        ),
        day_offsets=(0, 1, 4, 5),
    ),
}


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

OVERRIDE_FILE = BACKEND_ROOT / "payer_sources.json"


def _apply_overrides() -> None:
    if not OVERRIDE_FILE.exists():
        return
    try:
        data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("could not read %s; ignoring overrides", OVERRIDE_FILE)
        return

    for key, patch in (data or {}).items():
        if not isinstance(patch, dict):
            continue
        base = PAYER_SOURCES.get(key)
        if base is None:
            try:
                PAYER_SOURCES[key] = PayerSource(
                    key=key,
                    label=patch.get("label", key),
                    publisher_url=patch.get("publisher_url", ""),
                    resolver=patch.get("resolver", "templates"),
                    index_templates=tuple(patch.get("index_templates", ())),
                    index_url=patch.get("index_url"),
                    verified="override",
                    notes=patch.get("notes", "Added via payer_sources.json"),
                )
            except Exception:
                log.exception("bad override block for new payer %r", key)
            continue

        changes: dict = {"verified": "override"}
        for f in ("label", "publisher_url", "index_url", "notes", "resolver", "lookup_url"):
            if f in patch:
                changes[f] = patch[f]
        if "index_templates" in patch:
            changes["index_templates"] = tuple(patch["index_templates"])
        PAYER_SOURCES[key] = replace(base, **changes)

    log.info("applied payer source overrides from %s", OVERRIDE_FILE)


_apply_overrides()

PAYER_KEYS = tuple(PAYER_SOURCES.keys())


def get_source(key: str) -> PayerSource:
    src = PAYER_SOURCES.get((key or "").strip().lower())
    if src is None:
        raise KeyError(f"unknown payer {key!r}; known: {', '.join(PAYER_KEYS)}")
    return src


# ---------------------------------------------------------------------------
# Manually-downloaded local index files
# ---------------------------------------------------------------------------
#
# mrf.healthsparq.com (Aetna, MVP) refuses every automated client - see
# BLOCKED HOSTS above. Its per-employer-group index files (the small
# `{date}_{sponsor}_index.json.gz` TOCs, same CMS reporting_structure shape
# as any payer-wide index) can still be saved by hand from a browser or from
# whatever channel the payer provides, though. Drop them in
# `{DATA_ROOT}/local_indexes/{payer_key}/` and a search for that payer will
# fold every file found there into its candidate list instead of failing
# outright on "blocked". The files they reference still live on the blocked
# host, so scanning them will keep reporting errors until the payer lifts
# the IP block - but the search runs, and starts working the moment it does.


def local_index_dir(key: str) -> Path:
    return settings.data_root_path / "local_indexes" / (key or "").strip().lower()


def local_index_files(key: str) -> list[Path]:
    """Locally-provided *_index.json(.gz) files for this payer, oldest-name
    first. Empty when none have been dropped in - the normal case for every
    payer whose index resolves over the network."""
    d = local_index_dir(key)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in (".gz", ".json")
    )


# ---------------------------------------------------------------------------
# HTTP probing
# ---------------------------------------------------------------------------

# Imperva answers a refused client with a TLS alert rather than an HTTP
# status. urllib surfaces it as URLError(SSLError) with this text.
_TLS_DENIED_MARKERS = ("ACCESS_DENIED", "access denied", "LOGON_DENIED")


class HostBlocked(Exception):
    """The server refused the TLS handshake - a firewall policy, not a URL."""


def _classify(exc: BaseException) -> str:
    """Short, user-facing reason for a failed probe."""
    reason = getattr(exc, "reason", exc)
    text = f"{type(reason).__name__}: {reason}"
    if isinstance(reason, ssl.SSLError) or any(m in text for m in _TLS_DENIED_MARKERS):
        if any(m in text for m in _TLS_DENIED_MARKERS):
            return "blocked: host refused the TLS connection (firewall)"
        return f"tls: {reason}"
    if "getaddrinfo" in text or "Name or service not known" in text:
        return "dns: host does not resolve"
    if "timed out" in text.lower():
        return "timeout"
    return type(reason).__name__


def _is_blocked(err: str | None) -> bool:
    return bool(err) and err.startswith("blocked:")


def head(url: str, timeout: float = 12.0) -> tuple[bool, int | None, str | None]:
    """(ok, content_length, error). Falls back to a 1-byte ranged GET for hosts
    that reject HEAD (several payer CDNs do)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                cl = resp.headers.get("Content-Length")
                return True, int(cl) if cl and cl.isdigit() else None, None
            return False, None, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code not in (403, 405, 501):
            return False, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, None, _classify(e)

    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                rng = resp.headers.get("Content-Range", "")
                total = None
                if "/" in rng:
                    tail = rng.rsplit("/", 1)[-1]
                    if tail.isdigit():
                        total = int(tail)
                return True, total, None
            return False, None, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, None, _classify(e)


def sniff_json(url: str, timeout: float = 20.0) -> tuple[bool, str | None]:
    """Confirm the URL really serves a JSON document.

    A 200 alone is not enough: several payer portals are single-page apps
    that answer 200 with their HTML shell for *any* path. Pull the first
    bytes and require gzip magic or a JSON opening brace/bracket.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _UA, "Accept-Encoding": "identity", "Range": "bytes=0-2047"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            head_bytes = resp.read(2048)
    except Exception as e:  # noqa: BLE001
        return False, _classify(e)

    if head_bytes[:2] == b"\x1f\x8b":
        return True, None
    if "html" in ctype:
        return False, "served HTML, not JSON"
    if head_bytes.lstrip()[:1] in (b"{", b"["):
        return True, None
    return False, "body is not JSON"


def _fetch_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json,*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Candidate generation per resolver
# ---------------------------------------------------------------------------

def _dates(src: PayerSource) -> list[date]:
    """Posting dates to try, newest first."""
    out: list[date] = []
    d = date.today().replace(day=1)
    months: list[date] = []
    for _ in range(max(1, src.months_back)):
        months.append(d)
        d = (d - timedelta(days=1)).replace(day=1)
    today = date.today()
    for m in months:
        for off in src.day_offsets:
            cand = m + timedelta(days=off)
            if cand <= today and cand not in out:
                out.append(cand)
    # newest first so the current month's file wins
    return sorted(out, reverse=True)


def candidate_urls(src: PayerSource) -> list[str]:
    """Every URL we'd be willing to try, newest first, de-duplicated."""
    if src.index_url:
        return [src.index_url]

    seen: set[str] = set()
    out: list[str] = []

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            out.append(u)

    if src.resolver == "templates":
        for d in _dates(src):
            fields = {
                "ymd": d.strftime("%Y-%m-%d"),
                "Y": d.strftime("%Y"),
                "m": d.strftime("%m"),
                "d": d.strftime("%d"),
            }
            for tpl in src.index_templates:
                try:
                    add(tpl.format(**fields))
                except (KeyError, IndexError):
                    log.warning("bad template for %s: %r", src.key, tpl)
    elif src.resolver == "healthsparq":
        base = (
            f"https://mrf.healthsparq.com/{src.hsq_tenant}-egress.nophi.kyruushsq.com"
            f"/prd/mrf/{src.hsq_insurer}"
        )
        for d in _dates(src):
            ymd = d.strftime("%Y-%m-%d")
            for brand in src.hsq_brands:
                for entity in src.hsq_entities:
                    add(f"{base}/{brand}/{ymd}/tableOfContents/{ymd}_{entity}_index.json.gz")
    # "lookup" has no static candidates - see _resolve_lookup
    return out


def _resolve_lookup(src: PayerSource) -> tuple[str | None, int | None, list[dict], str | None]:
    """Cigna-style: fetch the lookup JSON and take the first TOC URL in it."""
    tried: list[dict] = []
    if not src.lookup_url:
        return None, None, tried, "no lookup_url configured"
    try:
        data = _fetch_json(src.lookup_url, timeout=settings.index_probe_timeout)
    except Exception as e:  # noqa: BLE001
        err = _classify(e)
        tried.append({"url": src.lookup_url, "ok": False, "error": err, "size_bytes": None})
        return None, None, tried, f"lookup failed: {err}"

    url: str | None = None
    for mrf in data.get("mrfs") or []:
        if str(mrf.get("kind", "TOC")).upper() != "TOC":
            continue
        for f in mrf.get("files") or []:
            if f.get("url"):
                url = str(f["url"])
                break
        if url:
            break
    if not url:
        tried.append({"url": src.lookup_url, "ok": False, "error": "no TOC url in lookup JSON", "size_bytes": None})
        return None, None, tried, "lookup JSON had no table-of-contents URL"

    ok, size, err = head(url, timeout=settings.index_probe_timeout)
    if ok:
        ok, err = sniff_json(url, timeout=settings.index_probe_timeout)
    tried.append({"url": url, "ok": ok, "error": err, "size_bytes": size})
    return (url if ok else None), size, tried, (None if ok else err)


# ---------------------------------------------------------------------------
# Resolution + cache
# ---------------------------------------------------------------------------

def _cache_file() -> Path:
    d = settings.data_root_path / "_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index_urls.json"


def _read_cache() -> dict:
    f = _cache_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(cache: dict) -> None:
    try:
        _cache_file().write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        log.exception("could not persist index URL cache")


def resolve_index_url(
    key: str,
    *,
    force: bool = False,
    probe_limit: int | None = None,
) -> dict:
    """Find the payer's live index URL.

    Returns {payer, label, url, size_bytes, resolved_at_ts, from_cache, tried,
             error, blocked, blocked_host, publisher_url, verified, notes}.
    `url` is None when nothing answered. `blocked` is True when the host
    refused the TLS handshake - the caller should tell the user the site's
    firewall is the problem, not the URL.
    """
    src = get_source(key)
    ttl = src.cache_ttl if src.cache_ttl is not None else settings.index_cache_ttl
    cache = _read_cache()
    hit = cache.get(src.key)
    if (
        not force
        and hit
        and hit.get("url")
        and (time.time() - hit.get("resolved_at_ts", 0)) < ttl
    ):
        return {**hit, "from_cache": True}

    limit = probe_limit if probe_limit is not None else settings.index_probe_limit
    tried: list[dict] = []
    found: str | None = None
    size: int | None = None
    error: str | None = None
    blocked_host: str | None = None

    if src.resolver == "lookup" and not src.index_url:
        found, size, tried, error = _resolve_lookup(src)
    else:
        # HealthSparq fans out to brands x entities x dates; a blocked host
        # answers every one of them identically and instantly, so stop at
        # the first refusal instead of burning the whole probe budget.
        hsq_limit = limit * 6 if src.resolver == "healthsparq" else limit
        for url in candidate_urls(src)[:hsq_limit]:
            host = urlsplit(url).netloc
            if blocked_host and host == blocked_host:
                continue
            ok, length, err = head(url, timeout=settings.index_probe_timeout)
            if ok:
                ok, err = sniff_json(url, timeout=settings.index_probe_timeout)
            tried.append({"url": url, "ok": ok, "error": err, "size_bytes": length})
            if ok:
                found, size = url, length
                break
            if _is_blocked(err):
                blocked_host = host
                break
        if not found:
            if blocked_host:
                error = (
                    f"{blocked_host} refused the connection (TLS access denied). "
                    "The publisher's firewall blocks automated clients; this is "
                    "not a wrong URL."
                )
            else:
                error = "No candidate index URL responded."

    entry = {
        "payer": src.key,
        "label": src.label,
        "url": found,
        "size_bytes": size,
        "resolved_at_ts": time.time(),
        "tried": tried,
        "error": None if found else error,
        "blocked": bool(blocked_host) and not found,
        "blocked_host": blocked_host if not found else None,
        "publisher_url": src.publisher_url,
        "verified": src.verified,
        "notes": src.notes,
    }
    if found:
        cache[src.key] = entry
        _write_cache(cache)
    return {**entry, "from_cache": False}


def pin_index_url(key: str, url: str) -> dict:
    """Persist a user-supplied absolute index URL into payer_sources.json.

    Verified first: a pinned URL that doesn't serve JSON would just move the
    failure from "search" to "every search", so refuse it with the reason.
    Raises ValueError with a user-facing message on failure.
    """
    src = get_source(key)
    ok, size, err = head(url, timeout=settings.index_probe_timeout)
    if ok:
        ok, err = sniff_json(url, timeout=settings.index_probe_timeout)
    if not ok:
        if _is_blocked(err):
            raise ValueError(
                f"{urlsplit(url).netloc} refused the connection from this server "
                f"(TLS access denied). Even pinned, the worker could not download "
                f"from it. {err}"
            )
        raise ValueError(f"That URL did not serve a JSON index ({err}).")

    data: dict = {}
    if OVERRIDE_FILE.exists():
        try:
            data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    block = data.get(src.key) or {}
    block["index_url"] = url
    data[src.key] = block
    OVERRIDE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    PAYER_SOURCES[src.key] = replace(src, index_url=url, verified="override")

    cache = _read_cache()
    cache.pop(src.key, None)
    _write_cache(cache)

    # a hand-pinned URL is the most trustworthy thing we have: put it in the
    # local store as `pinned` so it is used directly and never aged out
    from .index_store import put as store_put

    store_put(src.key, url, source="pinned", size_bytes=size, note="pinned by user")
    return resolve_index_url(src.key, force=True)


def unpin_index_url(key: str) -> None:
    """Drop a pinned URL and go back to the built-in resolver."""
    src = get_source(key)
    if OVERRIDE_FILE.exists():
        try:
            data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        block = data.get(src.key) or {}
        block.pop("index_url", None)
        if block:
            data[src.key] = block
        else:
            data.pop(src.key, None)
        OVERRIDE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    PAYER_SOURCES[src.key] = replace(src, index_url=None)
    cache = _read_cache()
    cache.pop(src.key, None)
    _write_cache(cache)

    from .index_store import forget as store_forget

    store_forget(src.key)
