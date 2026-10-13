"""
Cheap "which of these CPT/HCPCS codes actually appear in this file?" check.

This answers a different question than `provider_probe.py`: that module finds
one NPI/TIN and stops at the first hit; this one has to see every distinct
`billing_code` an `in_network` item carries, because a code near the end of
the file is exactly as real as one near the start.

It stays cheap by asking ijson for ONLY the `billing_code` scalar at
`in_network.item.billing_code` rather than the whole item the way
`extract_tic_all.py`'s real pass does. ijson still tokenizes every byte of
the file (there is no way around that in a single streaming pass), but it
never *materializes* the surrounding item - critically, never the
`negotiated_rates` array, which is where a national payer file's actual
weight lives. It also skips `build_provider_lookup()` entirely, which is
the single slowest phase of a real extraction (minutes, for tens of millions
of provider groups) and has nothing to do with this question. The result is
a scan that costs roughly one file download + one decompression pass, not a
full extraction.

Scanning stops the moment every requested code has been seen at least once -
seeing code X a second time never changes the answer "is X in this file?".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import ijson

from .net import open_url

log = logging.getLogger("mrf.cpt_probe")


@dataclass
class CptProbeResult:
    location: str
    requested_codes: list[str] = field(default_factory=list)
    matched_codes: list[str] = field(default_factory=list)
    items_scanned: int = 0
    bytes_scanned: int = 0
    file_size_bytes: int | None = None
    elapsed_seconds: float = 0.0
    # True once every requested code was found (scan stopped early) or the
    # file's in_network array was read to the end either way.
    exhausted: bool = False
    budget_exceeded: bool = False
    cancelled: bool = False
    error: str = ""
    # True for target_cpt_probe's discovery mode (codes=None): matched_codes
    # is "every code the target bills here", not a subset of requested_codes
    # (which is empty in that mode - there was nothing to request).
    discovery: bool = False

    @property
    def matched_count(self) -> int:
        return len(self.matched_codes)

    @property
    def requested_count(self) -> int:
        return len(self.requested_codes)

    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "requested_codes": self.requested_codes,
            "matched_codes": self.matched_codes,
            "matched_count": self.matched_count,
            "requested_count": self.requested_count,
            "items_scanned": self.items_scanned,
            "bytes_scanned": self.bytes_scanned,
            "file_size_bytes": self.file_size_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "exhausted": self.exhausted,
            "budget_exceeded": self.budget_exceeded,
            "cancelled": self.cancelled,
            "error": self.error,
            "discovery": self.discovery,
        }


def scan_billing_codes(
    location: str,
    codes: list[str],
    *,
    max_wire_bytes: int = 400 * 1024 * 1024,
    timeout_seconds: float = 300.0,
    http_timeout: float = 90.0,
    should_stop=None,
    on_progress=None,
) -> CptProbeResult:
    """Stream `location` and report which of `codes` appear as an
    `in_network.item.billing_code` value.

    `should_stop()` is polled between items so a cancelled request (client
    disconnect) stops promptly rather than finishing a 300MB read nobody is
    waiting on. Bounded by both a wire-byte budget and a wall-clock timeout,
    same discipline as `provider_probe.probe_file`, so one pathological file
    can never hang a request thread indefinitely.

    `on_progress(items_scanned, bytes_read, file_size_bytes)` fires roughly
    once a second, if given, so a caller can report real progress/ETA
    instead of a static upfront guess. `file_size_bytes` is the same value
    on every call (known once the response headers arrive) - passed each
    time rather than once so the caller doesn't need a separate hook for it.
    """
    wanted = {str(c).strip().upper() for c in codes if str(c).strip()}
    result = CptProbeResult(location=location, requested_codes=sorted(wanted))
    if not wanted:
        result.error = "no codes to check"
        return result

    started = time.monotonic()
    stream = counter = None
    matched: set[str] = set()
    last_report = started

    try:
        stream, counter, total = open_url(location, timeout=http_timeout)
        result.file_size_bytes = total

        for raw_code in ijson.items(stream, "in_network.item.billing_code"):
            result.items_scanned += 1
            code = str(raw_code).strip().upper()
            if code in wanted:
                matched.add(code)
                if matched == wanted:
                    result.exhausted = True
                    break

            if should_stop is not None and should_stop():
                result.cancelled = True
                break
            if (time.monotonic() - started) > timeout_seconds:
                result.budget_exceeded = True
                break
            if counter.bytes_read > max_wire_bytes:
                result.budget_exceeded = True
                break

            now = time.monotonic()
            if on_progress is not None and (now - last_report) >= 1.0:
                last_report = now
                on_progress(result.items_scanned, counter.bytes_read, result.file_size_bytes)
        else:
            # ijson's iterator ran out on its own: every in_network item in
            # the file has been seen.
            result.exhausted = True

        result.matched_codes = sorted(matched)
        result.bytes_scanned = counter.bytes_read if counter else 0
    except Exception as e:  # noqa: BLE001 - a dead URL is a result, not a crash
        result.matched_codes = sorted(matched)
        result.error = f"{type(e).__name__}: {e}"
        log.warning("cpt probe failed for %s: %s", location, e)
    finally:
        result.elapsed_seconds = time.monotonic() - started
        if counter is not None:
            try:
                counter.close()
            except Exception:
                pass

    return result
