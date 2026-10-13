"""
Shared ETA engine.

The naive `elapsed / done * remaining` estimate the job model started with is
fine for a steady single-phase loop, but this app's work is bursty (a stalled
HTTP read, an NPPES rate-limit pause, a stage boundary) and multi-phase, so
that estimate swings wildly and counts queue time as work time.

`RateEstimator` keeps an exponentially-weighted moving average of recent
throughput and blends it with the long-run average. Recent samples dominate
so the number reacts to a slowdown, while the long-run term keeps it from
whipsawing on one slow chunk. It also survives idle gaps: a sample with zero
progress decays the rate instead of pretending the old rate still holds.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

# Time constant of the moving average, in seconds: how fast the estimate
# forgets. Decay is TIME-weighted, not sample-weighted - a sample covering
# ten minutes must outweigh one covering ten seconds, or a job that slows
# down keeps reporting the optimistic rate it had when it started.
_TAU_SECONDS = 45.0
# Blend between EWMA (recent) and cumulative average (stable).
_EWMA_WEIGHT = 0.7
# Ignore samples shorter than this - they're noise, not signal.
_MIN_SAMPLE_SECONDS = 0.35


def _now() -> float:
    """The one clock everything here reads.

    A single indirection so a test can advance time deterministically;
    `time.monotonic` captured in a dataclass default_factory would be bound
    at import and silently ignore any patch, which is exactly the kind of
    half-patched clock that makes timing tests lie.
    """
    return time.monotonic()


@dataclass
class RateEstimator:
    """Units/second estimator. 'units' is whatever you're counting: bytes for
    a download, rows for enrichment, files for a search scan."""

    started_at: float = field(default_factory=lambda: _now())
    _ewma: float | None = field(default=None, init=False)
    _last_t: float = field(default_factory=lambda: _now(), init=False)
    _last_units: float = field(default=0.0, init=False)
    _total_units: float = field(default=0.0, init=False)

    def observe(self, units_done: float) -> None:
        """Record cumulative progress (not a delta)."""
        now = _now()
        dt = now - self._last_t
        if dt < _MIN_SAMPLE_SECONDS:
            return

        d_units = max(0.0, units_done - self._last_units)
        sample = d_units / dt

        # alpha grows with the span the sample covers: a long interval nearly
        # replaces the old estimate, a short one barely nudges it.
        alpha = 1.0 - math.exp(-dt / _TAU_SECONDS)
        self._ewma = sample if self._ewma is None else (
            alpha * sample + (1 - alpha) * self._ewma
        )
        self._last_t = now
        self._last_units = units_done
        self._total_units = max(self._total_units, units_done)

    @property
    def elapsed(self) -> float:
        return max(0.0, _now() - self.started_at)

    @property
    def rate(self) -> float:
        """Blended units/second. 0.0 until there's something to go on."""
        avg = (self._total_units / self.elapsed) if self.elapsed > 0 else 0.0
        if self._ewma is None:
            return avg
        return _EWMA_WEIGHT * self._ewma + (1 - _EWMA_WEIGHT) * avg

    def eta_seconds(self, units_total: float | None) -> float | None:
        """Seconds remaining, or None when it can't be known yet."""
        if not units_total or units_total <= 0:
            return None
        remaining = max(0.0, units_total - self._total_units)
        if remaining <= 0:
            return 0.0
        r = self.rate
        if r <= 0:
            return None
        return round(remaining / r, 1)


@dataclass
class PhasedEta:
    """ETA across a pipeline whose phases occupy known fractions of the whole.

    Each phase reports its own 0..1 completion; overall progress is the sum of
    finished phase weights plus the running phase's weighted fraction. The ETA
    is then derived from overall progress, so a fast phase 1 doesn't make the
    whole job look nearly done.

        eta = PhasedEta({"download": 0.25, "extract": 0.45,
                         "enrich": 0.22, "package": 0.08})
        eta.set_phase("download")
        eta.observe_phase(0.4)     -> overall 0.10
    """

    weights: dict[str, float]
    _order: list[str] = field(init=False)
    _phase: str | None = field(default=None, init=False)
    _phase_frac: float = field(default=0.0, init=False)
    _est: RateEstimator = field(default_factory=RateEstimator, init=False)

    def __post_init__(self) -> None:
        total = sum(self.weights.values()) or 1.0
        self.weights = {k: v / total for k, v in self.weights.items()}
        self._order = list(self.weights.keys())

    def set_phase(self, phase: str) -> None:
        if phase not in self.weights:
            raise KeyError(f"unknown phase {phase!r}; known: {self._order}")
        self._phase = phase
        self._phase_frac = 0.0

    def observe_phase(self, frac: float) -> None:
        self._phase_frac = max(0.0, min(1.0, frac))
        self._est.observe(self.overall_fraction)

    @property
    def overall_fraction(self) -> float:
        if self._phase is None:
            return 0.0
        done = 0.0
        for name in self._order:
            if name == self._phase:
                break
            done += self.weights[name]
        return min(1.0, done + self.weights[self._phase] * self._phase_frac)

    @property
    def percent(self) -> float:
        return round(100.0 * self.overall_fraction, 1)

    def eta_seconds(self) -> float | None:
        return self._est.eta_seconds(1.0)


def format_eta(seconds: float | None) -> str:
    """Human ETA for log lines. The UI formats its own from the raw number."""
    if seconds is None:
        return "unknown"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h, rem = divmod(s, 3600)
    if h < 24:
        return f"{h}h {rem // 60}m"
    d, rh = divmod(h, 24)
    return f"{d}d {rh}h"


# ---------------------------------------------------------------------------
# Time-based pipeline plan
# ---------------------------------------------------------------------------
#
# Fixed phase weights ("download is 22% of the job") are wrong for almost
# every real job: a narrowed extraction spends nothing on enrichment, a
# whole-network one spends hours there. So instead each phase gets an
# estimated DURATION from the file size and the throughput observed on past
# jobs, the running phase is projected from its own live rate, and percent is
# time-proportional. Throughput is learned per phase (and per scope for the
# enrichment-heavy extract pass) and persisted, so the second job of a kind
# is predicted far better than the first.

import json as _json
from pathlib import Path as _Path

_MB = 1024 * 1024
# bytes/second priors, used until real jobs have been observed
_RATE_PRIORS: dict[str, float] = {
    "download": 10 * _MB,
    "lookup": 6 * _MB,
    "extract:narrowed": 2.5 * _MB,
    "extract:all": 0.8 * _MB,
    "package": 40 * _MB,
    # target_cpt_probe.py's discover-all scan (Provider Lookup's "Discover
    # billed codes"): a lean ijson pass with no row/CSV building, so it's
    # much closer to the "lookup" phase's throughput than a narrowed
    # extraction's - reusing "extract:narrowed" here inflated its ETA a lot.
    "discover": 6 * _MB,
}
_RATE_ALPHA = 0.35  # weight of the newest job in the learned rate


def _rates_file() -> _Path:
    from .config import settings

    d = settings.data_root_path / "_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "phase_rates.json"


def load_phase_rates() -> dict[str, float]:
    rates = dict(_RATE_PRIORS)
    try:
        f = _rates_file()
        if f.exists():
            saved = _json.loads(f.read_text(encoding="utf-8"))
            rates.update({k: float(v) for k, v in saved.items() if v and float(v) > 0})
    except Exception:
        pass
    return rates


def learn_phase_rate(key: str, size_bytes: int, seconds: float) -> None:
    """Fold one finished phase into the learned throughput for `key`."""
    if not size_bytes or not seconds or seconds <= 0:
        return
    observed = size_bytes / seconds
    rates = load_phase_rates()
    prior = rates.get(key)
    rates[key] = observed if prior is None else (_RATE_ALPHA * observed + (1 - _RATE_ALPHA) * prior)
    try:
        f = _rates_file()
        current = {}
        if f.exists():
            current = _json.loads(f.read_text(encoding="utf-8"))
        current[key] = rates[key]
        f.write_text(_json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass


@dataclass
class _PhaseState:
    est_seconds: float
    started: float | None = None
    finished: float | None = None
    frac: float = 0.0
    rate: RateEstimator | None = None

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        end = self.finished if self.finished is not None else _now()
        return max(0.0, end - self.started)


class PhasePlan:
    """Percent + countdown for a pipeline whose phases have estimated durations.

        plan = PhasePlan([("download", 60), ("lookup", 90), ("extract", 240), ("package", 8)])
        plan.start("download"); plan.observe(0.4); ...; plan.finish("download")
        plan.percent()        -> time-proportional 0..100
        plan.eta_seconds()    -> remaining time, live-rate for the running phase

    The running phase's remaining time blends its live rate with the prior
    estimate, trusting the live rate more the longer the phase has run - the
    first few seconds of a phase are too noisy to extrapolate from alone.
    """

    def __init__(self, phases: list[tuple[str, float]]):
        self.order = [n for n, _ in phases]
        self.phases = {n: _PhaseState(est_seconds=max(1.0, float(s))) for n, s in phases}
        self.current: str | None = None

    # -- lifecycle -------------------------------------------------------

    def set_estimate(self, name: str, seconds: float) -> None:
        self.phases[name].est_seconds = max(1.0, float(seconds))

    def start(self, name: str) -> None:
        if self.current and self.current != name:
            self.finish(self.current)
        p = self.phases[name]
        if p.started is None:
            p.started = _now()
            p.rate = RateEstimator()
        self.current = name

    def observe(self, frac: float) -> None:
        if self.current is None:
            return
        p = self.phases[self.current]
        p.frac = max(p.frac, min(1.0, frac))  # never goes backwards
        if p.rate is not None:
            p.rate.observe(p.frac)

    def finish(self, name: str) -> None:
        p = self.phases[name]
        if p.started is None:
            p.started = _now()
        if p.finished is None:
            p.finished = _now()
        p.frac = 1.0
        # the actual duration replaces the estimate for percent bookkeeping
        p.est_seconds = max(1.0, p.elapsed)
        if self.current == name:
            self.current = None

    def actual_seconds(self, name: str) -> float | None:
        p = self.phases[name]
        return p.elapsed if p.finished is not None else None

    # -- readouts --------------------------------------------------------

    def _remaining_current(self) -> float:
        if self.current is None:
            return 0.0
        p = self.phases[self.current]
        left = 1.0 - p.frac
        prior = p.est_seconds * left
        live = None
        if p.rate is not None and p.frac > 0:
            r = p.rate.rate  # fraction per second
            if r > 0:
                live = left / r
        if live is None:
            return prior
        # trust the live rate more as the phase matures (fully after ~45s)
        w = min(1.0, p.elapsed / 45.0)
        return w * live + (1 - w) * prior

    def eta_seconds(self) -> float | None:
        if self.current is None and all(p.finished for p in self.phases.values()):
            return 0.0
        total = self._remaining_current()
        seen_current = self.current is None
        for name in self.order:
            if name == self.current:
                seen_current = True
                continue
            if not seen_current:
                continue  # earlier, finished phase
            p = self.phases[name]
            if p.finished is None:
                total += p.est_seconds
        return round(max(0.0, total), 1)

    def fraction(self) -> float:
        total = sum(p.est_seconds for p in self.phases.values()) or 1.0
        done = 0.0
        for name in self.order:
            p = self.phases[name]
            if p.finished is not None:
                done += p.est_seconds
            elif name == self.current:
                done += p.est_seconds * p.frac
        return max(0.0, min(1.0, done / total))

    def percent(self) -> float:
        return round(100.0 * self.fraction(), 1)
