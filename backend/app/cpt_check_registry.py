"""
Background runner for the "Check codes" probe on a search match.

Answers "of the codes I've picked, how many does the SEARCHED TIN/NPI itself
bill in this file?" - not "do these codes appear anywhere in the file for any
provider" (that would say yes for a code some unrelated practice in the
network bills, which is not the question a TIN/NPI search is asking). See
target_cpt_probe.py for why that needs provider-group resolution, not just a
billing_code scan.

This runs the scan in a background thread the moment it's requested, keyed
by match_id, so the caller can:
  * poll for live progress (phase, items scanned, matched codes so far, ETA)
  * cancel it, which sets a threading.Event the scan checks every ~1-5s via
    `should_stop` - the scan actually stops within a few seconds, not
    "whenever it happens to finish".

State lives in memory only (not the DB): a check is a cheap, re-runnable
probe, not a durable record like a search or extraction job, so losing it on
a server restart is fine - the user just clicks "Check codes" again. The
downloaded scratch copy of the file is always removed when the run ends.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cpt_probe import CptProbeResult
from .eta import PhasePlan, learn_phase_rate, load_phase_rates
from .target_cpt_probe import scan_target_billing_codes

log = logging.getLogger("mrf.cpt_check")

_lock = threading.Lock()
_runs: dict[str, "_Run"] = {}


def _est(rates: dict, key: str, nbytes: int, floor: float = 2.0) -> float:
    rate = rates.get(key) or 1.0
    return max(floor, (nbytes or 0) / rate)


def _scratch_path(match_id: str, company: str, filename: str) -> Path:
    """Where the file is downloaded to for the duration of one check - never
    the job's own raw/{company}/ path, so a check never collides with (or
    leaves multi-GB debris alongside) a real extraction's download."""
    from .config import settings
    from .storage import safe_filename

    d = settings.data_root_path / "_cptcheck" / safe_filename(match_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / safe_filename(filename)


@dataclass
class _Run:
    match_id: str
    location: str
    requested_codes: list[str]
    target_kind: str
    target_value: str
    target_group_id: str | None
    dest_path: Path
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    status: str = "running"  # running | done | error | cancelled
    phase: str = ""
    items_scanned: int = 0
    file_size_bytes: int | None = None
    matched_codes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    result: CptProbeResult | None = None
    error: str | None = None
    plan: PhasePlan | None = field(default=None, init=False)


def _build_plan(run: _Run) -> PhasePlan:
    """Same time-based phase plan run_real() uses for extraction, seeded
    from the same learned throughput file - this check runs the same
    download + provider-lookup work a narrowed extraction does, so day-one
    estimates are already grounded in real job history rather than a
    from-scratch guess."""
    rates = load_phase_rates()
    size = run.file_size_bytes or 0
    phases: list[tuple[str, float]] = []
    if not run.dest_path.exists():
        phases.append(("download", _est(rates, "download", size)))
    phases.append(("lookup", _est(rates, "lookup", size)))
    phases.append(("scan", _est(rates, "extract:narrowed", size)))
    return PhasePlan(phases)


def _run_body(run: _Run) -> None:
    plan = _build_plan(run)
    with _lock:
        run.plan = plan
        run.phase = plan.order[0]
        plan.start(run.phase)

    def _on_progress(phase: str, frac: float, meta: dict) -> None:
        with _lock:
            if phase not in plan.phases:
                return
            if phase != run.phase:
                plan.start(phase)
                run.phase = phase
            plan.observe(frac)
            fsb = meta.get("file_size_bytes")
            if fsb:
                run.file_size_bytes = fsb
            if "items_scanned" in meta:
                run.items_scanned = meta["items_scanned"]

    try:
        result = scan_target_billing_codes(
            run.location,
            run.dest_path,
            run.requested_codes,
            target_kind=run.target_kind,
            target_value=run.target_value,
            target_group_id=run.target_group_id,
            should_stop=run.stop_event.is_set,
            on_progress=_on_progress,
        )
        with _lock:
            for name in plan.order:
                if plan.phases[name].finished is None:
                    plan.finish(name)
            run.result = result
            run.matched_codes = result.matched_codes
            run.items_scanned = result.items_scanned
            run.file_size_bytes = result.file_size_bytes or run.file_size_bytes
            run.status = "cancelled" if result.cancelled else ("error" if result.error else "done")
            run.error = result.error or None

        # Feed real durations back into the shared learned-rate file so the
        # next check (or extraction) starts from a better prior. Only for
        # phases that always run to completion regardless of early-exit;
        # "scan" stops the moment the target's codes are all found, so its
        # duration isn't comparable to a full narrowed extraction's and is
        # deliberately not folded in.
        if result.file_size_bytes and not result.cancelled and not result.error:
            for name in ("download", "lookup"):
                secs = plan.actual_seconds(name)
                if secs:
                    learn_phase_rate(name, result.file_size_bytes, secs)
    except Exception as e:  # noqa: BLE001 - surfaced to the poller, not raised in a thread
        with _lock:
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"


def start(
    match_id: str,
    location: str,
    codes: list[str],
    *,
    target_kind: str,
    target_value: str,
    target_group_id: str | None = None,
    company: str,
    filename: str,
    known_file_size_bytes: int | None = None,
) -> None:
    """Launch (or replace) the check for this match. Any previous run for
    the same match_id is asked to stop first - a fresh click always wins
    over a stale scan of an earlier code selection.

    `target_kind` / `target_value` come from the search this match belongs
    to (npi|tin) - every check answers "does THIS provider bill these codes
    here", never "do these codes appear anywhere in the file". `target_group_id`
    (SearchMatch.provider_group_id, when the byte-scan captured one) narrows
    further to that one contract, since the same TIN/NPI can carry several
    in one file.

    `known_file_size_bytes` is the size the search already recorded for this
    match (from the payer's table of contents or an earlier probe) - seeding
    it means an ETA can show up immediately instead of waiting for the
    download's own response headers to confirm the same number.
    """
    with _lock:
        existing = _runs.get(match_id)
    if existing is not None and existing.status == "running":
        existing.stop_event.set()
        if existing.thread is not None:
            existing.thread.join(timeout=5.0)

    dest_path = _scratch_path(match_id, company, filename)
    run = _Run(
        match_id=match_id,
        location=location,
        requested_codes=codes,
        target_kind=target_kind,
        target_value=target_value,
        target_group_id=target_group_id,
        dest_path=dest_path,
        file_size_bytes=known_file_size_bytes,
    )
    thread = threading.Thread(target=_run_body, args=(run,), daemon=True, name=f"cpt-check-{match_id[:8]}")
    run.thread = thread
    with _lock:
        _runs[match_id] = run
    thread.start()


def cancel(match_id: str) -> bool:
    """Ask a running check to stop. Returns False if nothing is running."""
    with _lock:
        run = _runs.get(match_id)
    if run is None or run.status != "running":
        return False
    run.stop_event.set()
    return True


def status(match_id: str) -> dict | None:
    """Current snapshot for polling, or None if nothing has ever run for
    this match_id."""
    with _lock:
        run = _runs.get(match_id)
        if run is None:
            return None
        elapsed = time.monotonic() - run.started_at
        eta = run.plan.eta_seconds() if (run.plan is not None and run.status == "running") else None

        base = {
            "match_id": run.match_id,
            "status": run.status,
            "phase": run.phase or None,
            "items_scanned": run.items_scanned,
            "bytes_scanned": run.file_size_bytes or 0,
            "file_size_bytes": run.file_size_bytes,
            "matched_codes": sorted(run.matched_codes),
            "requested_codes": sorted(run.requested_codes),
            "elapsed_seconds": round(elapsed, 2),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "error": run.error,
        }
        if run.result is not None:
            base["exhausted"] = run.result.exhausted
            base["budget_exceeded"] = run.result.budget_exceeded
        return base


def forget(match_id: str) -> None:
    with _lock:
        _runs.pop(match_id, None)
