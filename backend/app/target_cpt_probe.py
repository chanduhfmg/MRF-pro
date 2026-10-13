"""
"Of these CPT/HCPCS codes, which ones does THIS provider actually bill in
this file?" - the question the Search page's "CPT matches" column exists to
answer, and the one thing `cpt_probe.scan_billing_codes` does NOT answer: that
module reports a code as matched the instant it appears anywhere in the file,
for any provider, which silently says "yes" for a code the searched TIN/NPI
never bills a cent for as long as some other practice in the network does.

Answering the real question requires the same provider-group resolution the
real extraction already does: a `billing_code` only counts once its
`negotiated_rates` entry is traced through `provider_references` to a
sub-group whose TIN/NPI matches the target. So this module is a thin wrapper
around `extract_tic_all.py`'s own `build_provider_lookup` / `iter_rate_rows`
(the exact functions the real pipeline uses) rather than a second
implementation that could silently drift from what extraction actually does.

Those functions read a local path (ijson streams a file object, not a URL),
so a match's remote `location` is downloaded to a scratch file first -
deleted again once the check finishes, since this is a cheap, re-runnable
preview, not a durable artifact. That download is the real cost increase
over the old whole-file byte scan: there is no way to know whether the
target bills a code without resolving provider groups across the whole file,
which means reading all of it exactly once for the lookup pass and once more
(narrowed early-exit) for the in_network pass - the same two passes a
narrowed real extraction runs, minus NPPES enrichment and CSV packaging.
"""
from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .cpt_probe import CptProbeResult
from .net import open_url

log = logging.getLogger("mrf.cpt_probe.target")


class CheckCancelled(Exception):
    """Raised from inside a progress callback to unwind download / ijson
    passes promptly when the user clicks Stop - the same pattern
    pipeline.context.ProgressReporter.check_cancel() uses for jobs."""


def _load_extract_mod():
    from .config import REPO_ROOT

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import extract_tic_all as extract_mod  # type: ignore

    return extract_mod


_MAX_NPIS_PER_GROUP = 20000


def _discover_matching_group_ids(
    extract_mod, dest_path: Path, target_kind: str, wanted_norm: str,
    *, target_group_id: str | None = None, progress=None, check_stop=None,
) -> set:
    """Discovery-only pass 1: which provider_group_ids have at least one
    sub-group matching the target - just the id set, not the full
    gid -> [{tin, npis}] map `build_provider_lookup` builds for row output.

    A file can carry hundreds of thousands of provider groups; when the only
    question is "does one of them match this one TIN/NPI", building and
    keeping the full lookup for all of them is pure waste. Same schema
    handling as build_provider_lookup (inline groups, external "location"
    pointers, the npi==tin corruption guard) so the match set here is
    identical to what a full lookup would resolve to - just without ever
    materializing the other ~99.9% of it.

    `target_group_id`, when given, narrows further: the same TIN/NPI can sit
    under several different provider_group_id entries in one file (separate
    contracts/networks for the same tax id), each potentially billing a
    different set of codes. Restricting to the one group_id the initial
    search hit actually landed on (SearchMatch.provider_group_id) answers
    "what does *this* contract bill", not "what does this TIN bill anywhere
    under any of its contracts here".
    """
    import ijson

    normalize_id = extract_mod.normalize_id
    matching: set = set()
    pending_urls: dict = {}
    total_size = dest_path.stat().st_size

    raw, stream_cm = extract_mod.open_json_stream(str(dest_path))
    with stream_cm as f:
        parser = ijson.parse(f)
        in_target_array = False
        cur: dict | None = None
        cur_group: dict | None = None
        count = 0
        last_heartbeat = time.monotonic()

        for prefix, event, value in parser:
            if check_stop is not None:
                check_stop()
            now = time.monotonic()
            if progress is not None and (now - last_heartbeat) >= 1.0:
                last_heartbeat = now
                progress(bytes_read=raw.tell(), total_bytes=total_size, groups=count)

            if prefix == "provider_references" and event == "start_array":
                in_target_array = True
                continue
            if not in_target_array:
                continue
            if prefix == "provider_references" and event == "end_array":
                break

            if prefix == "provider_references.item" and event == "start_map":
                cur = {"gid": None, "location": None, "matched": False}
                continue
            if prefix == "provider_references.item" and event == "end_map":
                count += 1
                if cur["location"]:
                    pending_urls[cur["gid"]] = cur["location"]
                elif cur["matched"] and (
                    target_group_id is None or str(cur["gid"]) == str(target_group_id)
                ):
                    matching.add(cur["gid"])
                cur = None
                continue
            if cur is None:
                continue

            if prefix == "provider_references.item.provider_groups.item" and event == "start_map":
                cur_group = {"tin": None, "npis": []}
                continue
            if prefix == "provider_references.item.provider_groups.item" and event == "end_map":
                if cur_group is not None:
                    tin_norm = normalize_id(cur_group["tin"] or "")
                    raw_npis = cur_group["npis"]
                    # same corrupt-subgroup guard extraction applies: an NPI
                    # identical to its own group's TIN means bad data, so the
                    # whole subgroup is not evidence of a match either way.
                    corrupt = any(normalize_id(str(n)) == tin_norm for n in raw_npis)
                    if not corrupt:
                        if target_kind == "tin" and tin_norm == wanted_norm:
                            cur["matched"] = True
                        elif target_kind == "npi" and any(
                            normalize_id(str(n)) == wanted_norm for n in raw_npis
                        ):
                            cur["matched"] = True
                cur_group = None
                continue

            if prefix == "provider_references.item.provider_group_id":
                cur["gid"] = value
            elif prefix == "provider_references.item.location":
                cur["location"] = value
            elif prefix.endswith(".provider_groups.item.npi.item"):
                if cur_group is not None and len(cur_group["npis"]) < _MAX_NPIS_PER_GROUP:
                    cur_group["npis"].append(value)
            elif prefix.endswith(".provider_groups.item.tin.value"):
                if cur_group is not None:
                    cur_group["tin"] = value
    raw.close()

    if pending_urls:
        import urllib.request

        for gid, url in pending_urls.items():
            if target_group_id is not None and str(gid) != str(target_group_id):
                continue  # not the contract we're narrowing to - skip the fetch entirely
            if check_stop is not None:
                check_stop()
            try:
                with urllib.request.urlopen(url) as resp:
                    groups = list(ijson.items(resp, "provider_groups.item"))
            except Exception:
                continue
            for g in groups:
                tin_val = (g.get("tin") or {}).get("value")
                tin_norm = normalize_id(tin_val or "")
                raw_npis = g.get("npi", [])
                if any(normalize_id(str(n)) == tin_norm for n in raw_npis):
                    continue
                if target_kind == "tin" and tin_norm == wanted_norm:
                    matching.add(gid)
                    break
                if target_kind == "npi" and any(normalize_id(str(n)) == wanted_norm for n in raw_npis):
                    matching.add(gid)
                    break

    return matching


def _iter_discovered_codes(
    extract_mod, dest_path: Path, matching_group_ids: set, *, progress=None, check_stop=None,
):
    """Discovery-only pass 2: yield each DISTINCT billing_code the instant a
    negotiated_rate under it resolves to a matching provider group.

    No row building: no item_fields copy, no negotiated_prices loop (a code
    can carry dozens of price entries per provider group - each one would be
    a wasted extra "row" in iter_rate_rows when the only thing being asked is
    "is this code billed at all", not "what does it pay"), no extra_fields
    JSON, no NPI list formatting. Once a code is confirmed, later items
    carrying the same code are skipped without even checking their
    provider_references - there is nothing left to learn from them.
    """
    import ijson

    total_size = dest_path.stat().st_size
    raw, stream_cm = extract_mod.open_json_stream(str(dest_path))
    seen: set = set()
    count = 0
    last_heartbeat = time.monotonic()

    with stream_cm as f:
        for item in ijson.items(f, "in_network.item"):
            count += 1
            if check_stop is not None:
                check_stop()
            now = time.monotonic()
            if progress is not None and (now - last_heartbeat) >= 1.0:
                last_heartbeat = now
                progress(bytes_read=raw.tell(), total_bytes=total_size, items=count)

            code = str(item.get("billing_code", "")).strip().upper()
            if not code or code in seen:
                continue
            for rate_entry in item.get("negotiated_rates", []):
                refs = rate_entry.get("provider_references", [])
                if any(gid in matching_group_ids for gid in refs):
                    seen.add(code)
                    yield code
                    break
    raw.close()


# on_progress(phase, frac, meta) - phase is "download" | "lookup" | "scan";
# frac is 0..1 within that phase (best-effort; "scan" can't know its own
# fraction ahead of the ijson pass finishing, so it reports 0.0 and lets the
# caller drive its own rate estimate off meta["items_scanned"]).
OnProgress = Callable[[str, float, dict], None]


def scan_target_billing_codes(
    location: str,
    dest_path: Path,
    codes: list[str] | None,
    *,
    target_kind: str,
    target_value: str,
    target_group_id: str | None = None,
    download_timeout: float = 300.0,
    timeout_seconds: float = 1800.0,
    should_stop: Callable[[], bool] | None = None,
    on_progress: OnProgress | None = None,
) -> CptProbeResult:
    """Download `location` to `dest_path` if not already there, then report
    which codes are billed by `target_kind`/`target_value` (npi|tin) in that
    file - not merely present in it for some other provider.

    `codes=None` means discovery mode: don't check a fixed list, return
    EVERY code the target bills here (no restriction passed to
    iter_rate_rows). There's no early-exit possible in that mode - the file
    has to be read to the end to know "every" code has been seen - so it
    costs a full narrowed extraction pass rather than an early-stoppable one.
    A non-None `codes` keeps the original behaviour: stop the instant every
    one of them has been seen under the target.

    `target_group_id`, when given, narrows further to that one
    provider_group_id: the same TIN/NPI can carry several separate contracts
    in one file, each its own provider_group_id with its own billed codes,
    and "what does this TIN bill anywhere" can overstate what one specific
    contract (the one a search hit actually landed on) bills. None keeps the
    original behaviour: any group matching the TIN/NPI counts.

    `dest_path` is always removed before returning, success or not.
    """
    discover_all = codes is None
    wanted = None if discover_all else {str(c).strip().upper() for c in codes if str(c).strip()}
    result = CptProbeResult(location=location, requested_codes=sorted(wanted) if wanted else [])
    result.discovery = discover_all
    if not discover_all and not wanted:
        result.error = "no codes to check"
        return result
    if target_kind not in ("npi", "tin") or not target_value:
        result.error = "no search target to scope the check to"
        return result

    started = time.monotonic()

    def _check_stop() -> None:
        if should_stop is not None and should_stop():
            raise CheckCancelled()
        if (time.monotonic() - started) > timeout_seconds:
            raise TimeoutError("cpt check exceeded its time budget")

    rows_gen = None
    codes_gen = None
    try:
        extract_mod = _load_extract_mod()

        if not dest_path.exists():
            # Goes through net.open_url() - the same single chokepoint every
            # other remote read in this app uses (provider_probe's byte scan,
            # the TOC listing) - rather than a second network path, so the
            # same test doubles/mocks cover this too. `counter` (not the
            # gzip-wrapping `stream`) is read here: it yields the RAW wire
            # bytes exactly as sent, so the file lands on disk still
            # compressed - extract_tic_all.py's open_json_stream() sniffs the
            # gzip magic itself, so this needs no naming convention.
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            _, counter, total = open_url(location, timeout=download_timeout)
            try:
                with open(tmp, "wb") as f:
                    while True:
                        _check_stop()
                        chunk = counter.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        if on_progress:
                            on_progress(
                                "download", (counter.bytes_read / total) if total else 0.0,
                                {"bytes_read": counter.bytes_read, "file_size_bytes": total},
                            )
                tmp.replace(dest_path)
            finally:
                try:
                    counter.close()
                except Exception:
                    pass
                tmp.unlink(missing_ok=True)  # no-op once replace() succeeded

        result.file_size_bytes = dest_path.stat().st_size

        def _lookup_progress(**kw) -> None:
            _check_stop()
            tb = kw.get("total_bytes") or result.file_size_bytes
            frac = (kw.get("bytes_read", 0) / tb) if tb else 0.0
            if on_progress:
                on_progress("lookup", frac, {"groups": kw.get("groups", 0)})

        matched: set[str] = set()
        exhausted = False
        items_scanned = 0

        if discover_all:
            # Lean, purpose-built passes (see _discover_matching_group_ids /
            # _iter_discovered_codes above): no row building, no
            # negotiated_prices loop, no per-row NPI/extra-field work - this
            # mode only needs a set of codes, and iter_rate_rows was built to
            # produce CSV rows, not that. Meaningfully faster on files with
            # large provider-reference sections or many prices per code.
            wanted_norm = extract_mod.normalize_id(str(target_value))
            matching_gids = _discover_matching_group_ids(
                extract_mod, dest_path, target_kind, wanted_norm,
                target_group_id=target_group_id,
                progress=_lookup_progress, check_stop=_check_stop,
            )

            def _scan_progress(**kw) -> None:
                _check_stop()
                nonlocal items_scanned
                items_scanned = kw.get("items", 0)
                tb = kw.get("total_bytes") or result.file_size_bytes
                frac = (kw.get("bytes_read", 0) / tb) if tb else 0.0
                if on_progress:
                    on_progress("scan", frac, {"items_scanned": items_scanned})

            codes_gen = _iter_discovered_codes(
                extract_mod, dest_path, matching_gids,
                progress=_scan_progress, check_stop=_check_stop,
            )
            for code in codes_gen:
                matched.add(code)  # no early exit - has to see the whole file
            exhausted = True  # ran to the end (or _check_stop raised)
        else:
            provider_lookup = extract_mod.build_provider_lookup(str(dest_path), progress=_lookup_progress)

            def _scan_progress(**kw) -> None:
                _check_stop()
                nonlocal items_scanned
                items_scanned = kw.get("items", 0)
                # `kw["matched_codes"]` here counts codes seen ANYWHERE in
                # the file among the wanted list, before target narrowing -
                # exactly the whole-file semantics this module exists to
                # replace, so it is deliberately not surfaced.
                tb = kw.get("total_bytes") or result.file_size_bytes
                frac = (kw.get("bytes_read", 0) / tb) if tb else 0.0
                if on_progress:
                    on_progress("scan", frac, {"items_scanned": items_scanned})

            stats: dict = {}
            rows_gen = extract_mod.iter_rate_rows(
                str(dest_path),
                provider_lookup=provider_lookup,
                target_kind=target_kind,
                target_value=target_value,
                target_codes=sorted(wanted),
                progress=_scan_progress,
                stats=stats,
            )

            # Built from the rows actually YIELDED (i.e. already resolved to
            # a provider sub-group matching the target) - NOT from
            # stats["matched_codes"], which `iter_rate_rows` populates the
            # instant a billing_code is seen anywhere in the file, before the
            # target check runs. iter_rate_rows only narrows by TIN/NPI, not
            # by provider_group_id, so a specific-group narrowing is applied
            # here on its output instead of touching extract_tic_all.py.
            for row in rows_gen:
                _check_stop()
                if target_group_id is not None and str(row.get("provider_group_id")) != str(target_group_id):
                    continue
                code = str(row.get("billing_code", "")).strip().upper()
                if code in wanted:
                    matched.add(code)
                    if matched == wanted:
                        exhausted = True
                        break
            else:
                exhausted = True  # in_network array read to the end
            items_scanned = stats.get("items_scanned", items_scanned)

        result.matched_codes = sorted(matched)
        result.exhausted = exhausted
        result.items_scanned = items_scanned
        result.bytes_scanned = result.file_size_bytes or 0
    except CheckCancelled:
        result.cancelled = True
    except TimeoutError:
        result.budget_exceeded = True
    except Exception as e:  # noqa: BLE001 - a dead URL/bad file is a result, not a crash
        result.error = f"{type(e).__name__}: {e}"
        log.warning("target cpt probe failed for %s (%s=%s): %s", location, target_kind, target_value, e)
    finally:
        if rows_gen is not None:
            try:
                rows_gen.close()
            except Exception:
                pass
        if codes_gen is not None:
            try:
                codes_gen.close()
            except Exception:
                pass
        result.elapsed_seconds = time.monotonic() - started
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass

    return result
