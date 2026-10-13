"""
Background runner for "Discover billed codes" on a Provider Lookup match.

Unlike the Search page's "Check codes" (a fixed list, checked cheaply against
the whole file) or its own automatic search-time verification (a fixed list,
narrowed to the target), this answers a different question with no fixed
list at all: "what does this TIN/NPI bill in this file, period?" - full
discovery, via target_cpt_probe.scan_target_billing_codes(codes=None).

Same background-thread/poll/cancel shape as cpt_check_registry.py (see that
module for why), duplicated rather than shared because the two answer
genuinely different questions and a discovery run has no early-exit (it has
to read the whole file to know it has seen "every" code, so its progress/ETA
profile is also different - always a full narrowed-extraction pass, never
short-circuited).
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

log = logging.getLogger("mrf.code_discovery")

_lock = threading.Lock()
_runs: dict[str, "_Run"] = {}


def _est(rates: dict, key: str, nbytes: int, floor: float = 2.0) -> float:
    rate = rates.get(key) or 1.0
    return max(floor, (nbytes or 0) / rate)


def _scratch_path(match_id: str, company: str, filename: str) -> Path:
    from .config import settings
    from .storage import safe_filename

    d = settings.data_root_path / "_discover" / safe_filename(match_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / safe_filename(filename)


@dataclass
class _Run:
    match_id: str
    location: str
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
    discovered_codes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    result: CptProbeResult | None = None
    error: str | None = None
    plan: PhasePlan | None = field(default=None, init=False)


def _build_plan(run: _Run) -> PhasePlan:
    """No "scan" early-exit in discovery mode - the whole in_network array is
    always read - so its prior is the full narrowed-extraction throughput,
    same as a real job's "extract:narrowed" phase, not a partial-scan guess."""
    rates = load_phase_rates()
    size = run.file_size_bytes or 0
    phases: list[tuple[str, float]] = []
    if not run.dest_path.exists():
        phases.append(("download", _est(rates, "download", size)))
    phases.append(("lookup", _est(rates, "lookup", size)))
    phases.append(("scan", _est(rates, "discover", size)))
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
            None,  # discovery mode: no fixed code list
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
            run.discovered_codes = result.matched_codes
            run.items_scanned = result.items_scanned
            run.file_size_bytes = result.file_size_bytes or run.file_size_bytes
            run.status = "cancelled" if result.cancelled else ("error" if result.error else "done")
            run.error = result.error or None

        if result.file_size_bytes and not result.cancelled and not result.error:
            # Unlike the fixed-codes check (cpt_check_registry.py), discovery
            # never early-exits - it always reads the file to the end - so
            # its "scan" duration is comparable run to run and worth learning
            # from too, not just "download"/"lookup".
            for name in ("download", "lookup", "scan"):
                secs = plan.actual_seconds(name)
                if secs:
                    key = "discover" if name == "scan" else name
                    learn_phase_rate(key, result.file_size_bytes, secs)
    except Exception as e:  # noqa: BLE001 - surfaced to the poller, not raised in a thread
        with _lock:
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"


def start(
    match_id: str,
    location: str,
    *,
    target_kind: str,
    target_value: str,
    target_group_id: str | None = None,
    company: str,
    filename: str,
    known_file_size_bytes: int | None = None,
) -> None:
    """Launch (or replace) discovery for this match. Any previous run for the
    same match_id is asked to stop first.

    `target_group_id` (SearchMatch.provider_group_id, when the byte-scan
    captured one) narrows discovery to that one contract instead of any
    provider group carrying the target TIN/NPI - the same TIN/NPI can carry
    several separate contracts in one file, each with its own billed codes.
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
        target_kind=target_kind,
        target_value=target_value,
        target_group_id=target_group_id,
        dest_path=dest_path,
        file_size_bytes=known_file_size_bytes,
    )
    thread = threading.Thread(target=_run_body, args=(run,), daemon=True, name=f"discover-{match_id[:8]}")
    run.thread = thread
    with _lock:
        _runs[match_id] = run
    thread.start()


def cancel(match_id: str) -> bool:
    with _lock:
        run = _runs.get(match_id)
    if run is None or run.status != "running":
        return False
    run.stop_event.set()
    return True


def status(match_id: str) -> dict | None:
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
            "file_size_bytes": run.file_size_bytes,
            "discovered_codes": sorted(run.discovered_codes),
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
