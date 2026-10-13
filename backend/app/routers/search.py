"""
/api/searches + /api/payers — the NPI/TIN → in-network files → extraction flow.

    POST /api/searches                  queue a search        -> SearchOut
    GET  /api/searches/{id}/status      poll it (ETA here)    -> SearchStatusOut
    GET  /api/searches/{id}/matches     the files it found    -> MatchListOut
    POST /api/searches/{id}/extract     turn picks into jobs  -> ExtractSelectionOut

The search itself never runs in the request path: it queues a ProviderSearch
row and the polling worker picks it up, exactly like an extraction. That is
what keeps a 40-file network scan from holding an HTTP connection open.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import code_discovery_registry, cpt_check_registry, crud, storage
from ..config import settings
from ..constants import INSURANCE_COMPANY_KEYS
from ..database import get_db
from ..models import JobStatus
from ..payers import PAYER_SOURCES, pin_index_url, resolve_index_url, unpin_index_url
from ..schemas import (
    CptCheckIn,
    CptCheckStatusOut,
    DiscoverCodesStatusOut,
    ExtractSelectionIn,
    ExtractSelectionOut,
    JobOut,
    MatchListOut,
    MatchOut,
    PayerSourceOut,
    PinIndexIn,
    SearchCreateIn,
    SearchListOut,
    SearchOut,
    SearchStatusOut,
)

log = logging.getLogger("mrf.api.search")

router = APIRouter(prefix="/api", tags=["search"])


# --------------------------------------------------------------------------
# Payer MRF sources
# --------------------------------------------------------------------------

@router.get("/payers", response_model=list[PayerSourceOut])
def list_payers(
    probe: bool = Query(
        False,
        description="Actually HEAD the candidate index URLs. Slower, but tells "
                    "you which payers are reachable right now.",
    ),
    force: bool = Query(False, description="Bypass the resolution cache."),
) -> list[PayerSourceOut]:
    return [
        _payer_out(key, _resolve_for_output(key, force=force) if probe else None)
        for key in PAYER_SOURCES
    ]


def _resolve_for_output(key: str, *, force: bool = False) -> dict:
    """Turn a resolver exception into an unavailable payer response.

    Reaching a payer-owned MRF host is inherently unreliable.  That must be
    represented as ``resolved=False`` in the UI, never as an API 500.
    """
    try:
        return resolve_index_url(key, force=force)
    except Exception as exc:  # noqa: BLE001 - external publisher failed
        src = PAYER_SOURCES[key]
        log.warning("could not resolve payer index for %s: %s", key, exc)
        return {
            "payer": key,
            "label": src.label,
            "url": None,
            "size_bytes": None,
            "from_cache": False,
            "blocked": False,
            "blocked_host": None,
            "error": f"Can't find the payer MRF file: {type(exc).__name__}: {exc}",
            "tried": [],
        }


def _payer_out(key: str, r: dict | None) -> PayerSourceOut:
    from ..index_store import get as store_get, staleness

    src = PAYER_SOURCES[key]
    entry = store_get(key)
    reason = staleness(entry)
    base = {
        "key": key,
        "label": src.label,
        "publisher_url": src.publisher_url,
        "verified": src.verified,
        "notes": src.notes,
        "pinned": bool(src.index_url) or (entry.source == "pinned" if entry else False),
        "stored_url": entry.url if entry else None,
        "stored_at": entry.resolved_at if entry else None,
        "stored_source": entry.source if entry else None,
        "stored_stale": bool(reason) if entry else None,
        "stored_stale_reason": reason,
        "reporting_month": entry.reporting_month if entry else None,
    }
    if r is None:
        # no probe: report what the store knows, which is what a search uses
        return PayerSourceOut(
            **base,
            index_url=entry.url if entry else src.index_url,
            size_bytes=entry.size_bytes if entry else None,
            resolved=bool(entry and entry.url),
            from_cache=True,
        )
    return PayerSourceOut(
        **base,
        index_url=r.get("url"),
        size_bytes=r.get("size_bytes"),
        resolved=bool(r.get("url")),
        from_cache=bool(r.get("from_cache")),
        blocked=bool(r.get("blocked")),
        blocked_host=r.get("blocked_host"),
        error=r.get("error"),
        tried=r.get("tried", []),
    )


@router.get("/payers/{key}", response_model=PayerSourceOut)
def get_payer(
    key: str,
    probe: bool = Query(
        False,
        description="Hit the network to re-resolve. Off by default: the stored "
                    "URL is what a search actually uses.",
    ),
    force: bool = Query(False),
) -> PayerSourceOut:
    """One payer's source status - what the Search page checks before you
    click Search, so a blocked or moved index is known up front.

    Reads the local store by default, so this is instant and works offline.
    """
    if key not in PAYER_SOURCES:
        raise HTTPException(404, f"Unknown payer '{key}'")
    return _payer_out(key, _resolve_for_output(key, force=force) if probe else None)


@router.post("/payers/refresh")
def refresh_payer_indexes(
    force: bool = Query(False, description="Re-resolve even entries that are still fresh."),
) -> dict:
    """Re-resolve payer index URLs and store them locally.

    Runs automatically on API startup for stale entries, and monthly by
    virtue of INDEX_REFRESH_DAYS. This is the manual button for 'a payer
    just re-published and I don't want to wait'.
    """
    from ..index_store import refresh_all

    results = refresh_all(force=force, only_stale=not force)
    refreshed = sum(1 for r in results if r["action"] == "refreshed")
    failed = sum(1 for r in results if r["action"] == "failed")
    log.info("index refresh: %d refreshed, %d failed", refreshed, failed)
    return {"refreshed": refreshed, "kept": len(results) - refreshed - failed,
            "failed": failed, "results": results}


@router.post("/payers/{key}/index", response_model=PayerSourceOut)
def pin_payer_index(key: str, body: PinIndexIn) -> PayerSourceOut:
    """Pin an exact index URL for a payer, persisted to payer_sources.json.

    The escape hatch for the month a payer moves its bucket: paste the current
    table-of-contents link from the publisher page and searches work again
    with no redeploy. The URL is verified to serve JSON before it is saved.
    """
    if key not in PAYER_SOURCES:
        raise HTTPException(404, f"Unknown payer '{key}'")
    url = (body.index_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "index_url must be an http(s) URL")
    try:
        r = pin_index_url(key, url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    log.info("pinned index URL for %s: %s", key, url)
    return _payer_out(key, r)


@router.delete("/payers/{key}/index", response_model=PayerSourceOut)
def unpin_payer_index(key: str) -> PayerSourceOut:
    """Remove a pinned URL and return to the built-in resolver."""
    if key not in PAYER_SOURCES:
        raise HTTPException(404, f"Unknown payer '{key}'")
    unpin_index_url(key)
    return _payer_out(key, _resolve_for_output(key, force=True))


# --------------------------------------------------------------------------
# Searches
# --------------------------------------------------------------------------

def _validate_company(key: str) -> str:
    key = (key or "").strip().lower()
    if key not in INSURANCE_COMPANY_KEYS:
        raise HTTPException(422, f"Unknown insurance_company '{key}'")
    return key


def _normalize(search_type: str, value: str) -> tuple[str, str]:
    """(display_value, normalized_value), rejecting obviously wrong lengths.

    An NPI is exactly 10 digits and a TIN/EIN exactly 9, so a typo is caught
    here rather than after a 20-minute scan that was never going to hit.
    """
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        raise HTTPException(422, "search_value must contain digits")

    if search_type == "npi" and len(digits) != 10:
        raise HTTPException(
            422, f"An NPI is 10 digits; got {len(digits)} ('{value}')."
        )
    if search_type == "tin" and len(digits) != 9:
        raise HTTPException(
            422, f"A TIN/EIN is 9 digits; got {len(digits)} ('{value}')."
        )

    return digits, (digits.lstrip("0") or "0")


@router.post("/searches", response_model=SearchOut)
def create_search(body: SearchCreateIn, db: Session = Depends(get_db)) -> SearchOut:
    company = _validate_company(body.insurance_company)
    stype = body.search_type.value
    display, norm = _normalize(stype, body.search_value)

    search = crud.create_search(
        db,
        insurance_company=company,
        search_type=stype,
        search_value=display,
        search_value_norm=norm,
        hint=(body.hint or "").strip() or None,
        hint_strict=body.hint_strict,
        files_to_scan=body.max_files or settings.search_max_files,
    )
    log.info(
        "queued search %s: %s %s=%s (scan up to %d files)",
        search.id, company, stype, display, search.files_to_scan,
    )
    return SearchOut.from_search(search)


@router.get("/searches", response_model=SearchListOut)
def list_searches(
    company: str | None = Query(None),
    status: JobStatus | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> SearchListOut:
    rows, total = crud.list_searches(
        db,
        company=company,
        status=status.value if status else None,
        limit=limit,
        offset=offset,
    )
    return SearchListOut(
        searches=[SearchOut.from_search(s) for s in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/searches/{search_id}", response_model=SearchOut)
def get_search(search_id: str, db: Session = Depends(get_db)) -> SearchOut:
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    return SearchOut.from_search(s)


@router.get("/searches/{search_id}/status", response_model=SearchStatusOut)
def get_search_status(search_id: str, db: Session = Depends(get_db)) -> SearchStatusOut:
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    return SearchStatusOut.from_search(s)


# Throughput used to turn a file size into an ETA before any work starts.
# Deliberately conservative: an over-promise here is worse than a pleasant
# surprise, and it is replaced by the job's real measured ETA within seconds
# of the job starting.
_EST_BYTES_PER_SECOND = 3.0 * 1024 * 1024


def _estimate_seconds(size_bytes: int | None) -> float | None:
    if not size_bytes or size_bytes <= 0:
        return None
    return round(size_bytes / _EST_BYTES_PER_SECOND, 1)


def _match_out(m) -> MatchOut:
    out = MatchOut.from_match(m)
    out.estimated_seconds = _estimate_seconds(m.file_size_bytes)
    return out


@router.get("/searches/{search_id}/matches", response_model=MatchListOut)
def get_matches(
    search_id: str,
    matched_only: bool = Query(True, description="Hide files that were ruled out."),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> MatchListOut:
    """One page of a search's results. Safe to poll while the search is still
    running - rows appear as files are scanned."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")

    page, total = crud.list_matches(
        db, search_id, matched_only=matched_only, limit=limit, offset=offset
    )
    # Whole-search stats are independent of the page and the filter.
    all_rows, _ = crud.list_matches(db, search_id)
    # Same "worth downloading" definition list_matches(matched_only=True) uses:
    # matched AND not a same-network-refiled duplicate of an earlier match.
    # Counting duplicates here would make matched_count disagree with the
    # actual number of files the matched_only list (and total) returns.
    matched_unique = [
        m for m in all_rows if m.matched and m.duplicate_of_match_id is None
    ]
    known = [
        e for e in (_estimate_seconds(m.file_size_bytes) for m in matched_unique)
        if e is not None
    ]

    return MatchListOut(
        search=SearchOut.from_search(s),
        matches=[_match_out(m) for m in page],
        total=total,
        limit=limit,
        offset=offset,
        matched_count=len(matched_unique),
        scanned_count=len(all_rows),
        estimated_total_seconds=round(sum(known), 1) if known else None,
    )


@router.post("/searches/{search_id}/matches/{match_id}/cpt-check", response_model=CptCheckStatusOut)
def start_match_cpt_check(
    search_id: str,
    match_id: str,
    body: CptCheckIn,
    db: Session = Depends(get_db),
) -> CptCheckStatusOut:
    """Before committing to a multi-minute extraction: of the codes I've
    picked, how many does the searched TIN/NPI itself actually bill in this
    one file? (Not "does this code appear anywhere in the file for any
    provider" - resolved through provider groups, same as extraction would.)

    Launches the scan in a background thread and returns immediately with
    its initial (running) status - poll with GET on this same path for live
    progress, or DELETE to stop it. A large file can take well over a
    minute; blocking the request on it left the caller with no real
    progress and no way to abort, which this replaces.

    Downloads the file and resolves provider groups, so it costs roughly a
    narrowed extraction's first two phases rather than a plain byte scan -
    that is the price of a target-scoped answer instead of a whole-file one.
    Stops the moment every requested code has been seen under the target.
    """
    from .code_sets import clean_codes

    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    codes: list[str] | None = None
    if body.code_set_id:
        cs = crud.get_code_set(db, body.code_set_id)
        if not cs:
            raise HTTPException(404, "Code set not found")
        codes = cs.codes or None
    if body.codes:
        codes = clean_codes(body.codes) or None
    if not codes:
        raise HTTPException(422, "Provide codes or code_set_id.")

    stype = s.search_type.value if hasattr(s.search_type, "value") else str(s.search_type)
    cpt_check_registry.start(
        match_id,
        m.location,
        codes,
        target_kind=stype,
        target_value=s.search_value,
        # The byte-scan may have already pinned this hit to one specific
        # provider_group_id (a "strong" confidence match) - narrowing to it
        # answers "what does this exact contract bill", not "what does this
        # TIN bill under any of its contracts in this file". A "weak" match
        # (no group context found) leaves this None, falling back to the
        # broader any-group behaviour.
        target_group_id=m.provider_group_id or None,
        company=s.insurance_company,
        filename=m.filename,
        known_file_size_bytes=m.file_size_bytes,
    )
    log.info(
        "search %s match %s: cpt check started for %d code(s), scoped to %s=%s%s",
        search_id, match_id, len(codes), stype, s.search_value,
        f" group {m.provider_group_id}" if m.provider_group_id else "",
    )
    return _cpt_status_out(match_id, codes)


@router.get("/searches/{search_id}/matches/{match_id}/cpt-check", response_model=CptCheckStatusOut)
def poll_match_cpt_check(search_id: str, match_id: str, db: Session = Depends(get_db)) -> CptCheckStatusOut:
    """Live progress for the check started above. Once `status` is "done",
    the result is persisted onto the match (same as before) so reopening the
    search re-serves it instead of re-scanning."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    snap = cpt_check_registry.status(match_id)
    if snap is None:
        raise HTTPException(404, "No check has been started for this match")

    if snap["status"] in ("done", "error", "cancelled") and not m.cpt_checked_at:
        # First poll to see it finished: persist once, same as the old
        # synchronous path did, so history/reopens don't need a re-scan.
        crud.save_match_cpt_check(
            db, match_id,
            checked_codes=snap["requested_codes"],
            matched_codes=snap["matched_codes"],
            items_scanned=snap["items_scanned"],
            bytes_scanned=snap["bytes_scanned"],
            exhausted=bool(snap.get("exhausted")),
            budget_exceeded=bool(snap.get("budget_exceeded")),
            error=snap.get("error"),
        )
        log.info(
            "search %s match %s: cpt check %s (%d/%d code(s) matched)",
            search_id, match_id, snap["status"],
            len(snap["matched_codes"]), len(snap["requested_codes"]),
        )

    return CptCheckStatusOut(**snap)


@router.delete("/searches/{search_id}/matches/{match_id}/cpt-check", response_model=CptCheckStatusOut)
def cancel_match_cpt_check(search_id: str, match_id: str, db: Session = Depends(get_db)) -> CptCheckStatusOut:
    """Stop a running check. The background thread notices within about a
    second (same polling discipline as a search's Stop button) rather than
    running to completion regardless."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    cpt_check_registry.cancel(match_id)
    log.info("search %s match %s: cpt check stop requested", search_id, match_id)
    snap = cpt_check_registry.status(match_id)
    if snap is None:
        raise HTTPException(404, "No check has been started for this match")
    return CptCheckStatusOut(**snap)


def _cpt_status_out(match_id: str, requested_codes: list[str]) -> CptCheckStatusOut:
    snap = cpt_check_registry.status(match_id)
    if snap is None:
        # Shouldn't happen right after start(), but keep the response shape
        # honest if the thread hasn't registered itself yet.
        return CptCheckStatusOut(
            match_id=match_id, status="running", requested_codes=sorted(requested_codes),
            matched_codes=[], items_scanned=0, bytes_scanned=0, elapsed_seconds=0.0,
        )
    return CptCheckStatusOut(**snap)


# --------------------------------------------------------------------------
# Provider Lookup: "Discover billed codes" - full discovery, no fixed list
# --------------------------------------------------------------------------

@router.post(
    "/searches/{search_id}/matches/{match_id}/discover-codes",
    response_model=DiscoverCodesStatusOut,
)
def start_match_code_discovery(
    search_id: str, match_id: str, db: Session = Depends(get_db)
) -> DiscoverCodesStatusOut:
    """Every CPT/HCPCS code the search's own target TIN/NPI bills in this
    file - not a fixed list, full discovery. Downloads the file and resolves
    provider groups (same cost as a narrowed extraction's first two phases,
    plus a full in_network pass with no early exit), so this is meaningfully
    slower than the Search page's "Check codes" - there's no way to know
    "every" code without reading the whole file.

    Launches in the background and returns immediately; poll with GET on
    this same path, or DELETE to stop it.
    """
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    stype = s.search_type.value if hasattr(s.search_type, "value") else str(s.search_type)
    code_discovery_registry.start(
        match_id,
        m.location,
        target_kind=stype,
        target_value=s.search_value,
        # Narrow to the exact contract the byte-scan landed on, when known -
        # see the same note in start_match_cpt_check above.
        target_group_id=m.provider_group_id or None,
        company=s.insurance_company,
        filename=m.filename,
        known_file_size_bytes=m.file_size_bytes,
    )
    log.info(
        "search %s match %s: code discovery started for %s=%s%s",
        search_id, match_id, stype, s.search_value,
        f" group {m.provider_group_id}" if m.provider_group_id else "",
    )
    snap = code_discovery_registry.status(match_id)
    if snap is None:  # shouldn't happen right after start(), but keep it honest
        return DiscoverCodesStatusOut(
            match_id=match_id, status="running", discovered_codes=[],
            items_scanned=0, elapsed_seconds=0.0,
        )
    return DiscoverCodesStatusOut(**snap)


@router.get(
    "/searches/{search_id}/matches/{match_id}/discover-codes",
    response_model=DiscoverCodesStatusOut,
)
def poll_match_code_discovery(
    search_id: str, match_id: str, db: Session = Depends(get_db)
) -> DiscoverCodesStatusOut:
    """Live progress for the discovery run above. Once `status` is "done",
    the result is persisted onto the match so reopening the search re-serves
    it instead of re-scanning."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    snap = code_discovery_registry.status(match_id)
    if snap is None:
        raise HTTPException(404, "No discovery run has been started for this match")

    if snap["status"] in ("done", "error", "cancelled") and not m.target_codes_discovered_at:
        crud.save_match_discovered_codes(
            db, match_id,
            discovered_codes=snap["discovered_codes"],
            error=snap.get("error"),
        )
        log.info(
            "search %s match %s: code discovery %s (%d code(s) found)",
            search_id, match_id, snap["status"], len(snap["discovered_codes"]),
        )

    return DiscoverCodesStatusOut(**snap)


@router.delete(
    "/searches/{search_id}/matches/{match_id}/discover-codes",
    response_model=DiscoverCodesStatusOut,
)
def cancel_match_code_discovery(
    search_id: str, match_id: str, db: Session = Depends(get_db)
) -> DiscoverCodesStatusOut:
    """Stop a running discovery - noticed within about a second."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    m = crud.get_match(db, match_id)
    if not m or m.search_id != search_id:
        raise HTTPException(404, "Match not found on this search")

    code_discovery_registry.cancel(match_id)
    log.info("search %s match %s: code discovery stop requested", search_id, match_id)
    snap = code_discovery_registry.status(match_id)
    if snap is None:
        raise HTTPException(404, "No discovery run has been started for this match")
    return DiscoverCodesStatusOut(**snap)


@router.post("/searches/{search_id}/cancel", response_model=SearchOut)
def cancel_search(search_id: str, db: Session = Depends(get_db)) -> SearchOut:
    """"Stop process". The user already has the files they wanted; there is
    no point scanning the remaining ones. Matches found so far are kept and
    stay extractable."""
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    if s.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        raise HTTPException(409, f"Search already {s.status.value}; nothing to stop.")
    s = crud.request_search_cancel(db, search_id)
    log.info("search %s: stop requested (was %s)", search_id, s.status.value)
    return SearchOut.from_search(s)


@router.post("/searches/{search_id}/extract", response_model=ExtractSelectionOut)
def extract_selection(
    search_id: str,
    body: ExtractSelectionIn,
    db: Session = Depends(get_db),
) -> ExtractSelectionOut:
    """Create one queued extraction job per chosen file.

    The jobs carry `source_url`, so the worker downloads each file and then
    runs extraction and NPPES enrichment back to back with no further input -
    the user picks files once and the rest is automatic.
    """
    s = crud.get_search(db, search_id)
    if not s:
        raise HTTPException(404, "Search not found")

    matches, _ = crud.list_matches(db, search_id, matched_only=True)
    if not matches:
        raise HTTPException(409, "This search has no matching files to extract.")

    # Resolve which codes this batch extracts. Explicit list beats a saved
    # set; neither means the script's built-in default.
    from .code_sets import clean_codes

    codes: list[str] | None = None
    code_set_name: str | None = None
    if body.code_set_id:
        cs = crud.get_code_set(db, body.code_set_id)
        if not cs:
            raise HTTPException(404, "Code set not found")
        code_set_name = cs.name
        codes = cs.codes or None
    if body.codes:
        # explicit codes win; keep the set's name only if they still match it
        explicit = clean_codes(body.codes) or None
        if explicit != codes:
            code_set_name = None
        codes = explicit
    codes_json = json.dumps(codes) if codes else None

    by_id = {m.id: m for m in matches}
    if body.top_n:
        chosen = matches[: body.top_n]
    elif body.match_ids:
        missing = [i for i in body.match_ids if i not in by_id]
        if missing:
            raise HTTPException(
                422, f"Unknown or non-matching match_id(s): {', '.join(missing[:5])}"
            )
        chosen = [by_id[i] for i in body.match_ids]
    else:
        raise HTTPException(422, "Provide either match_ids or top_n.")

    stype = s.search_type.value if hasattr(s.search_type, "value") else str(s.search_type)

    jobs = []
    for m in chosen:
        if m.job_id:
            existing = crud.get_job(db, m.job_id)
            # Re-queueing a file that already succeeded would burn hours to
            # produce the same zip, so reuse it. A failed one is fair to retry.
            if existing and existing.status != JobStatus.failed:
                jobs.append(existing)
                continue

        dest = storage.raw_path(s.insurance_company, m.filename)
        if dest.exists():
            dest = dest.with_name(f"{m.id[:8]}_{dest.name}")

        job = crud.create_job(
            db,
            insurance_company=s.insurance_company,
            source_filename=dest.name,
            source_file_path=str(dest),
            total_bytes=m.file_size_bytes,
            source_url=m.location,
            search_id=s.id,
            match_id=m.id,
            target_type=stype,
            target_value=s.search_value,
            filter_to_target=body.filter_to_target,
            target_codes=codes_json,
            plan_name=m.plan_name or m.description,
            code_set_name=code_set_name,
        )
        crud.attach_job_to_match(db, m.id, job.id)
        jobs.append(job)

    est = [_estimate_seconds(m.file_size_bytes) for m in chosen]
    known = [e for e in est if e is not None]

    log.info("search %s: queued %d extraction job(s)", search_id, len(jobs))
    return ExtractSelectionOut(
        search_id=search_id,
        created=len(jobs),
        jobs=[JobOut.from_job(j) for j in jobs],
        estimated_total_seconds=round(sum(known), 1) if known else None,
    )


@router.get("/searches/{search_id}/jobs", response_model=list[JobOut])
def jobs_for_search(search_id: str, db: Session = Depends(get_db)) -> list[JobOut]:
    if not crud.get_search(db, search_id):
        raise HTTPException(404, "Search not found")
    return [JobOut.from_job(j) for j in crud.jobs_for_search(db, search_id)]
