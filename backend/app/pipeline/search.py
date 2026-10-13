"""
The provider-search pipeline: given a payer and an NPI or TIN, find which of
that payer's in-network files actually contain it.

    resolve index URL  ->  stream the TOC  ->  rank candidates  ->  probe N of
    them in parallel  ->  write one SearchMatch row per file examined

Runs inside the same polling worker as extraction, so it inherits the atomic
claim, the heartbeat, and the stuck-job requeue.

On scanning breadth
-------------------
A payer TOC can reference millions of in-network files, and each is multi-GB,
so "search every file" is not a thing that can exist. What this does instead
is bounded and honest: rank the candidates, scan the top `files_to_scan` of
them, and record exactly how many were examined so the result reads
"3 matches in 40 files scanned" rather than implying the whole payer was
searched. The UI surfaces both numbers and lets the user widen the scan.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
import time

from ..config import settings
from ..constants import (
    SEARCH_STAGE_LISTING,
    SEARCH_STAGE_RESOLVING,
    SEARCH_STAGE_SCANNING,
)
from ..eta import RateEstimator, format_eta
from ..mrf_index import iter_index_entries, iter_local_index_entries, rank_entries
from ..payers import get_source, local_index_dir, local_index_files, resolve_index_url
from ..provider_probe import probe_file

log = logging.getLogger("mrf.worker.search")


class SearchFailed(RuntimeError):
    pass


def run_search(search, report) -> dict:
    """Execute one ProviderSearch.

    `search` is a detached ORM row (read-only here). `report` is a
    SearchReporter that owns every DB write. Returns a summary dict.
    """
    company = search.insurance_company
    search_type = (
        search.search_type.value
        if hasattr(search.search_type, "value")
        else str(search.search_type)
    )
    value = search.search_value

    # ---- 1. Index URL: from the local store, not the network -------------
    # Stored answers are reused for INDEX_REFRESH_DAYS, so this is normally
    # instant and works even when the payer's host is unreachable.
    from ..index_store import get_index_url

    report.stage(SEARCH_STAGE_RESOLVING)
    got = get_index_url(company)
    index_url = got.get("url")
    # Cheap directory listing - checked up front so it's available as a
    # fallback no matter which path below hits a blocked/unreadable host,
    # not just the "nothing stored" one.
    local_files: list = local_index_files(company)
    using_local = False

    if index_url:
        if got["from_store"]:
            log.info(
                "search %s: index from local store%s",
                search.id, " (stale, refresh failed)" if got["stale"] else "",
            )
    else:
        resolved = resolve_index_url(company)
        label = resolved.get("label", company)
        if resolved.get("blocked"):
            if not local_files:
                raise SearchFailed(
                    f"{label} publishes its files on {resolved.get('blocked_host')}, "
                    f"which refused the connection (TLS access denied). That site's "
                    f"firewall blocks automated downloads - it is not a wrong URL, and "
                    f"retrying will not help. If the publisher page opens in your "
                    f"browser, download the table of contents there and pin a URL "
                    f"this server can reach; otherwise this payer cannot be searched "
                    f"from here. Alternatively, save index.json(.gz) files by hand "
                    f"into {local_index_dir(company)} and re-run - this search will "
                    f"use those instead. Publisher: {resolved.get('publisher_url')}"
                )
            using_local = True
            log.info(
                "search %s: %s's index host is blocked; using %d locally-provided "
                "index file(s) from %s instead",
                search.id, label, len(local_files), local_index_dir(company),
            )
        else:
            tried = ", ".join(t["url"] for t in resolved.get("tried", [])[:4])
            hint = ""
            if any("Refused" in str(t.get("error")) for t in resolved.get("tried", [])):
                hint = (
                    " Every candidate was refused at the network level, which usually "
                    "means this machine cannot reach the payer's host at all (firewall "
                    "or VPN policy) rather than the URL being wrong."
                )
            raise SearchFailed(
                f"Can't find the payer MRF file for {label}. "
                f"No usable index is stored locally. Tried: {tried or 'nothing'}.{hint} "
                f"Open {resolved.get('publisher_url')} , copy the current index link, "
                f"and use 'Pin index URL' below - it is saved locally and reused."
            )

    def _local_label(*, reason: str) -> str:
        return f"local: {len(local_files)} manually-downloaded index file(s) ({reason})"

    if using_local:
        index_url = _local_label(reason="index host is blocked")
    report.stage(SEARCH_STAGE_LISTING, index_url=index_url)
    log.info("search %s: index %s", search.id, index_url)

    # ---- 2. Stream the table of contents ---------------------------------
    try:
        toc_kind = get_source(company).toc_kind
    except KeyError:
        toc_kind = "cms_index"

    # 0 is deliberately unlimited: enumerate every eligible file in the
    # payer's table of contents. Keep a positive setting as an optional
    # operational safeguard for deployments that explicitly choose one.
    #
    # That safeguard exists for network-fetched TOCs that can run into the
    # millions of entries (Anthem's is ~10 GB). Two cases are exempt:
    #
    #   - Locally-provided index files are a small set someone hand-picked
    #     specifically because the network host is blocked - capping them
    #     defeats the point of providing them, and a payer with several
    #     local files can easily clear 200 entries before the file that
    #     actually matters is even opened.
    #
    #   - UHC's "blobs" listing is one API response with sizes already
    #     inline (~6,900 real in-network files after stub-filtering, not
    #     millions), and listing it fully is cheap - a cancelled run has
    #     reached all ~6,874 entries well before scanning even started.
    #     Capping it at 200 doesn't save meaningful time, but it does make
    #     results flaky: which 200 of 6,874 files land in the sample isn't
    #     stable run to run, so the same TIN can show 13 matches one day
    #     and 0 the next, from a file that was simply never looked at.
    cap = (
        None if (using_local or toc_kind == "uhc_blobs")
        else (settings.search_max_index_entries or None)
    )

    def _list_progress(**kw):
        if kw.get("done"):
            return
        report.listing(
            files_listed=kw.get("entries", 0),
            bytes_read=kw.get("bytes_read", 0),
            total_bytes=kw.get("total_bytes"),
        )

    def _collect(iterator) -> list:
        out = []
        for entry in iterator:
            out.append(entry)
            # Anthem's index is ~10 GB; a user who clicks Stop while we're
            # still reading it should not have to wait for the whole thing.
            if len(out) % 200 == 0 and report.cancelled(force=True):
                break
        return out

    try:
        if using_local:
            entries = _collect(
                iter_local_index_entries(local_files, max_entries=cap, progress=_list_progress)
            )
        else:
            entries = _collect(
                iter_index_entries(
                    index_url, max_entries=cap, progress=_list_progress, toc_kind=toc_kind
                )
            )
    except Exception as e:  # noqa: BLE001
        if using_local:
            raise SearchFailed(
                f"One of the local index files for {company} in "
                f"{local_index_dir(company)} could not be read ({e})."
            ) from e
        # A stored/resolved URL that can't be read is poison for the store:
        # it would fail every search until it ages out. Drop it so the next
        # attempt re-resolves, regardless of whether a local fallback saves
        # *this* run.
        if got.get("from_store"):
            from ..index_store import invalidate

            invalidate(company, f"index could not be read: {e}")
        if not local_files:
            if got.get("from_store"):
                raise SearchFailed(
                    f"The stored index URL for {company} could not be read ({e}). "
                    f"It has been discarded - run the search again and a fresh URL "
                    f"will be resolved."
                ) from e
            raise SearchFailed(f"Failed reading the index at {index_url}: {e}") from e

        # A remote index that worked before (or just resolved) but can't be
        # read right now - typically the host going from reachable to
        # blocked between resolutions - still has a local fallback available.
        log.warning(
            "search %s: remote index unreadable (%s); falling back to %d local "
            "file(s)", search.id, e, len(local_files),
        )
        using_local = True
        index_url = _local_label(reason=f"remote index unreadable: {e}")
        report.stage(SEARCH_STAGE_LISTING, index_url=index_url)
        try:
            entries = _collect(
                iter_local_index_entries(local_files, max_entries=cap, progress=_list_progress)
            )
        except Exception as e2:  # noqa: BLE001
            raise SearchFailed(
                f"Local index files for {company} in {local_index_dir(company)} "
                f"could not be read either ({e2})."
            ) from e2

    if report.cancelled(force=True):
        log.info("search %s: stopped by user while listing (%d entries)", search.id, len(entries))
        return _summary(index_url, len(entries), 0, 0, 0), True

    if not entries:
        raise SearchFailed(
            f"The index at {index_url} listed no in-network files. The payer may "
            "have changed its table-of-contents format."
        )

    log.info("search %s: %d unique in-network files listed", search.id, len(entries))

    # A plain hint only reorders (matching files scanned first, nothing
    # excluded). hint_strict turns it into a real filter: "only the HMO
    # plans", not "HMO plans scanned first, then whatever's left". Applied
    # before ranking (and before the files_listed report, so that number
    # reflects what's actually in play, not the payer's whole catalog).
    if search.hint_strict and (search.hint or "").strip():
        needle = search.hint.strip().lower()
        filtered = [
            e for e in entries
            if needle in f"{e.description} {e.plan_name} {e.filename}".lower()
        ]
        if not filtered:
            raise SearchFailed(
                f"No files for {company} matched \"{search.hint}\" in their plan "
                "name, description, or filename. Try a shorter or different "
                "term, or turn off the strict filter to scan everything with "
                "matches merely prioritized."
            )
        log.info(
            "search %s: hint_strict %r kept %d of %d listed files",
            search.id, search.hint, len(filtered), len(entries),
        )
        entries = filtered

    report.listing(files_listed=len(entries), bytes_read=0, total_bytes=None, final=True)

    # ---- 3. Rank + take the slice we'll actually scan ---------------------
    ranked = rank_entries(
        entries,
        hint=search.hint or "",
        budget_bytes=settings.search_probe_max_bytes,
    )
    budget = search.files_to_scan or settings.search_max_files
    targets = ranked[: max(1, budget)]
    report.scanning_started(files_to_scan=len(targets))

    # ---- 4. Probe them, bounded-parallel ---------------------------------
    est = RateEstimator()
    lock = threading.Lock()
    state = {"scanned": 0, "matched": 0, "bytes": 0}
    # location -> (wire_bytes_read_so_far, expected_total) for files still
    # being read. Without this the progress bar cannot move until a whole
    # multi-hundred-MB file completes.
    inflight: dict[str, tuple[int, int]] = {}
    stop_flag = threading.Event()
    n_targets = len(targets)

    def _fraction_and_bytes() -> tuple[float, int]:
        """Overall scan progress in [0,1], plus bytes read so far."""
        with lock:
            completed = state["scanned"]
            done_bytes = state["bytes"]
            partial = 0.0
            live_bytes = 0
            for wire, total in inflight.values():
                live_bytes += wire
                if total > 0:
                    partial += min(1.0, wire / total)
        frac = (completed + partial) / n_targets if n_targets else 0.0
        return min(1.0, frac), done_bytes + live_bytes

    def _report_partial() -> None:
        frac, total_bytes = _fraction_and_bytes()
        est.observe(frac)
        with lock:
            matched = state["matched"]
            scanned = state["scanned"]
        report.scanning(
            files_scanned=scanned,
            files_matched=matched,
            bytes_scanned=total_bytes,
            scan_fraction=frac,
            eta_seconds=est.eta_seconds(1.0),
        )

    def _probe(entry):
        # Checked on entry, and against the DB not just the local flag: the
        # pool hands the next file to a thread the instant the previous one
        # finishes - before the main loop has seen the stop and set
        # stop_flag. Reading the flag here means a file that starts after
        # the user clicked Stop does no work at all.
        if stop_flag.is_set() or report.cancelled(force=True):
            stop_flag.set()
            return entry, None

        def _on_progress(wire: int, _decompressed: int) -> None:
            with lock:
                inflight[entry.location] = (wire, entry.size_bytes or 0)
            _report_partial()

        try:
            res = probe_file(
                entry.location,
                search_type,
                value,
                max_wire_bytes=settings.search_probe_max_bytes,
                timeout_seconds=settings.search_probe_timeout,
                should_stop=lambda: stop_flag.is_set() or report.cancelled(),
                on_progress=_on_progress,
            )
        finally:
            with lock:
                inflight.pop(entry.location, None)
        return entry, res

    workers = max(1, min(settings.search_concurrency, len(targets)))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_probe, e) for e in targets]
        try:
            for fut in cf.as_completed(futures):
                # A future we .cancel()'d below still comes out of
                # as_completed - and .result() on it raises CancelledError,
                # which would turn a clean user stop into a failed search.
                if fut.cancelled():
                    continue
                entry, res = fut.result()

                # "Stop process": the user already has the files they came
                # for. Set the flag so in-flight probes abort at their next
                # chunk and never-started ones are dropped, then let the
                # loop drain - the matches recorded so far are kept.
                if not stop_flag.is_set() and report.cancelled(force=True):
                    log.info("search %s: stop requested by user", search.id)
                    stop_flag.set()
                    for f in futures:
                        f.cancel()

                if res is None:
                    continue

                # A payer that republishes one network under a second legal
                # entity produces a second "match" that is not a new file
                # worth downloading - same TIN, near-identical size. Checked
                # here (single-threaded, as futures complete one at a time)
                # so two concurrently-finishing duplicates of each other
                # can't both claim to be the canonical one.
                dup_id = (
                    report.find_duplicate(matched_tin=res.matched_tin, file_size_bytes=entry.size_bytes or res.file_size_bytes)
                    if res.matched else None
                )

                with lock:
                    state["scanned"] += 1
                    state["bytes"] += res.bytes_scanned
                    if res.matched and not dup_id:
                        state["matched"] += 1
                    scanned = state["scanned"]
                    matched = state["matched"]
                    total_bytes = state["bytes"]

                report.match(entry, res, duplicate_of_match_id=dup_id)

                frac, live_bytes = _fraction_and_bytes()
                est.observe(frac)
                eta = est.eta_seconds(1.0)
                report.scanning(
                    files_scanned=scanned,
                    files_matched=matched,
                    bytes_scanned=max(total_bytes, live_bytes),
                    scan_fraction=frac,
                    eta_seconds=eta,
                    force=True,
                )
                log.info(
                    "search %s: %d/%d scanned, %d matched, %.0f%%, ETA %s",
                    search.id, scanned, n_targets, matched, 100 * frac, format_eta(eta),
                )
        finally:
            stop_flag.set()

    cancelled = report.cancelled(force=True)
    summary = _summary(
        index_url, len(entries), state["scanned"], state["matched"], state["bytes"]
    )
    log.info(
        "search %s %s: %s", search.id, "stopped by user" if cancelled else "complete", summary
    )
    return summary, cancelled


def _summary(index_url, listed, scanned, matched, bytes_scanned) -> dict:
    return {
        "index_url": index_url,
        "files_listed": listed,
        "files_scanned": scanned,
        "files_matched": matched,
        "bytes_scanned": bytes_scanned,
    }


class SearchReporter:
    """Owns every DB write for a running search, throttled the same way the
    extraction ProgressReporter is (a stage change always writes through)."""

    def __init__(self, search_id: str, min_interval: float = 1.5):
        self.search_id = search_id
        self.min_interval = min_interval
        self._last = 0.0
        self._cancelled = False
        self._last_cancel_check = 0.0

    # -- stage transitions -------------------------------------------------

    def stage(self, name: str, *, index_url: str | None = None) -> None:
        self._write(stage=name, index_url=index_url, force=True)

    def listing(
        self,
        *,
        files_listed: int,
        bytes_read: int,
        total_bytes: int | None,
        final: bool = False,
    ) -> None:
        stage = SEARCH_STAGE_LISTING
        if total_bytes and bytes_read and not final:
            pct = 100.0 * bytes_read / total_bytes
            stage = f"{SEARCH_STAGE_LISTING} — {pct:.0f}% of index read"
        self._write(stage=stage, files_listed=files_listed, force=final)

    def scanning_started(self, *, files_to_scan: int) -> None:
        self._write(
            stage=SEARCH_STAGE_SCANNING,
            files_to_scan=files_to_scan,
            files_scanned=0,
            files_matched=0,
            scan_fraction=0.0,
            force=True,
        )

    def scanning(
        self,
        *,
        files_scanned: int,
        files_matched: int,
        bytes_scanned: int,
        eta_seconds: float | None,
        scan_fraction: float | None = None,
        force: bool = False,
    ) -> None:
        self._write(
            files_scanned=files_scanned,
            files_matched=files_matched,
            bytes_scanned=bytes_scanned,
            scan_fraction=scan_fraction,
            eta_seconds=eta_seconds,
            force=force,
        )

    # -- results -----------------------------------------------------------

    def find_duplicate(self, *, matched_tin: str, file_size_bytes: int | None) -> str | None:
        """An earlier canonical match in this search with the same TIN and a
        near-identical size - see crud.find_duplicate_match. None on any
        failure (a missed dedup just means one extra file gets shown, never
        a hidden real match, so this fails open rather than raising)."""
        from ..database import session_scope
        from .. import crud

        try:
            with session_scope() as db:
                dup = crud.find_duplicate_match(
                    db, self.search_id,
                    matched_tin=matched_tin, file_size_bytes=file_size_bytes or 0,
                )
                return dup.id if dup else None
        except Exception:
            log.exception("duplicate check failed for search %s", self.search_id)
            return None

    def match(self, entry, res, *, duplicate_of_match_id: str | None = None) -> None:
        from ..database import session_scope
        from .. import crud

        try:
            with session_scope() as db:
                crud.record_match(
                    db,
                    self.search_id,
                    location=entry.location[:2000],
                    filename=entry.filename[:500],
                    description=(entry.description or "")[:1000] or None,
                    plan_name=(entry.plan_name or "")[:500] or None,
                    plan_id=(entry.plan_id or "")[:100] or None,
                    plan_market_type=(entry.plan_market_type or "")[:50] or None,
                    reference_count=entry.reference_count,
                    # The index's own size is authoritative when it has one
                    # (UHC); otherwise fall back to the probe's Content-Length.
                    file_size_bytes=entry.size_bytes or res.file_size_bytes,
                    matched=res.matched,
                    confidence=res.confidence or None,
                    provider_group_id=(res.provider_group_id or "")[:100] or None,
                    matched_tin=(res.matched_tin or "")[:40] or None,
                    npi_count=res.npi_count,
                    context=(res.context or "")[:4000] or None,
                    bytes_scanned=res.bytes_scanned,
                    scan_seconds=res.elapsed_seconds,
                    exhausted=res.exhausted,
                    budget_exceeded=res.budget_exceeded,
                    duplicate_of_match_id=duplicate_of_match_id,
                    error=(res.error or "")[:4000] or None,
                )
        except Exception:
            log.exception("could not record match for search %s", self.search_id)

    def cancelled(self, *, force: bool = False) -> bool:
        """Has the user asked us to stop?

        The answer is sticky - once cancelled, always cancelled. Probe
        threads poll this every chunk, so by default the DB read is throttled
        to once a second across all of them. Anything that *decides* on the
        answer (the per-file check, the final verdict) passes `force=True`:
        a stale False there would record a search the user stopped as
        "completed", which is exactly the bug the throttle must not cause.
        """
        if self._cancelled:
            return True
        now = time.time()
        if not force and (now - self._last_cancel_check) < 1.0:
            return False
        self._last_cancel_check = now

        from ..database import session_scope
        from .. import crud

        try:
            with session_scope() as db:
                self._cancelled = crud.is_search_cancel_requested(db, self.search_id)
        except Exception:
            log.exception("cancel check failed for search %s", self.search_id)
        return self._cancelled

    # -- internal ----------------------------------------------------------

    def _write(self, *, force: bool = False, **fields) -> None:
        now = time.time()
        if not force and (now - self._last) < self.min_interval:
            return
        self._last = now

        from ..database import session_scope
        from .. import crud

        try:
            with session_scope() as db:
                crud.update_search_progress(db, self.search_id, **fields)
        except Exception:
            log.exception("search progress write failed for %s", self.search_id)
