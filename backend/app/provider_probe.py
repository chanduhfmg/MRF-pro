"""
Decide whether a payer in-network-rates file contains a given NPI or TIN,
without downloading the whole thing.

WHY NOT JUST ijson THE provider_references
------------------------------------------
The clean answer would be to stream-parse `provider_references` and look for
the id. Two problems in practice:

  * Several payers (Cigna among them) emit `provider_references` entries as
    *pointers* to external files, so the id isn't in this file's bytes at all
    under that key.
  * Several payers put `provider_references` **after** the multi-GB
    `in_network` array, so an event-level parse has to chew through the entire
    file before reaching the answer.

So the probe is two-tier, cheapest first:

  Tier 1 - **byte scan.** Decompress the stream and look for the id's digits
    with digit-boundary anchoring (`(?<!\\d)1234567890(?!\\d)`), so a 10-digit
    NPI can't be matched inside a longer number. This is an order of magnitude
    faster than JSON parsing, works regardless of where in the file the id
    lives, and can abort the moment it hits. A TIN is additionally searched in
    its dashed `12-3456789` form.

  Tier 2 - **context confirm.** On a hit we already hold a window of
    surrounding bytes, so we regex the enclosing `tin` / `provider_group_id` /
    `npi` fragment out of it. That turns "these digits appear somewhere" into
    "this id sits in a provider group, and here is its group id", which is what
    the results table shows. A hit that yields no provider context is reported
    with `confidence="weak"` rather than being silently upgraded or dropped.

Every probe is bounded by a wire-byte budget and a wall-clock timeout so one
pathological file can never stall a search.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .net import open_url

log = logging.getLogger("mrf.probe")

_CHUNK = 1024 * 1024
# Longest possible match plus surrounding context we must not split across
# chunk boundaries.
_OVERLAP = 4096
# Bytes of context kept around a hit for the confirm pass.
_CTX_BEFORE = 6000
_CTX_AFTER = 3000

_RE_GROUP_ID = re.compile(rb'"provider_group_id"\s*:\s*"?([A-Za-z0-9_.\-]+)"?')
_RE_TIN_VALUE = re.compile(rb'"tin"\s*:\s*\{[^}]{0,200}?"value"\s*:\s*"([^"]{1,32})"')
_RE_NPI_ARRAY = re.compile(rb'"npi"\s*:\s*\[([^\]]{0,20000})\]')


def normalize_id(raw: str) -> str:
    """Digits only, leading zeros stripped - the same normalization
    extract_tic_all.py and enrich_npi_all.py use, so a match here means the
    same thing downstream."""
    digits = re.sub(r"\D", "", raw or "")
    return digits.lstrip("0") or "0"


@dataclass
class ProbeResult:
    location: str
    matched: bool = False
    # "strong" = id found inside a provider-group context;
    # "weak"   = digits found but no provider context nearby;
    # ""       = no match.
    confidence: str = ""
    provider_group_id: str = ""
    matched_tin: str = ""
    npi_count: int = 0
    context: str = ""
    bytes_scanned: int = 0
    decompressed_bytes: int = 0
    elapsed_seconds: float = 0.0
    exhausted: bool = False          # scanned the whole file, no match
    budget_exceeded: bool = False    # gave up on the byte/time budget
    error: str = ""
    file_size_bytes: int | None = None
    _hit_offset: int = field(default=0, repr=False)

    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "matched": self.matched,
            "confidence": self.confidence,
            "provider_group_id": self.provider_group_id,
            "matched_tin": self.matched_tin,
            "npi_count": self.npi_count,
            "context": self.context,
            "bytes_scanned": self.bytes_scanned,
            "decompressed_bytes": self.decompressed_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "exhausted": self.exhausted,
            "budget_exceeded": self.budget_exceeded,
            "error": self.error,
            "file_size_bytes": self.file_size_bytes,
        }


def _patterns(search_type: str, value: str) -> list[re.Pattern[bytes]]:
    """Digit-boundary-anchored needles for the id."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return []

    forms = {digits}
    # ids are stored both with and without leading zeros
    forms.add(digits.lstrip("0") or "0")
    if search_type == "npi" and len(digits) < 10:
        forms.add(digits.zfill(10))
    if search_type == "tin":
        if len(digits) < 9:
            forms.add(digits.zfill(9))
        if len(digits) == 9:
            forms.add(f"{digits[:2]}-{digits[2:]}")

    pats: list[re.Pattern[bytes]] = []
    for f in forms:
        esc = re.escape(f.encode())
        # A bare digit run must not be part of a longer number. The dashed TIN
        # form is already unambiguous.
        if "-" in f:
            pats.append(re.compile(esc))
        else:
            pats.append(re.compile(rb"(?<![0-9])" + esc + rb"(?![0-9])"))
    return pats


def probe_file(
    location: str,
    search_type: str,
    value: str,
    *,
    max_wire_bytes: int = 400 * 1024 * 1024,
    timeout_seconds: float = 300.0,
    http_timeout: float = 90.0,
    should_stop=None,
    on_progress=None,
) -> ProbeResult:
    """Scan one in-network file for `value`.

    `search_type` is "npi" or "tin". `should_stop()` is polled between chunks
    so a cancelled search stops promptly. `on_progress(wire_bytes,
    decompressed_bytes)` fires roughly once a second.
    """
    result = ProbeResult(location=location)
    pats = _patterns(search_type, value)
    if not pats:
        result.error = "empty search value"
        return result

    started = time.monotonic()
    stream = counter = None

    try:
        stream, counter, total = open_url(location, timeout=http_timeout, on_bytes=None)
        result.file_size_bytes = total

        tail = b""
        decompressed = 0
        last_report = started

        while True:
            if should_stop is not None and should_stop():
                result.budget_exceeded = True
                break

            now = time.monotonic()
            if (now - started) > timeout_seconds:
                result.budget_exceeded = True
                break
            if counter.bytes_read > max_wire_bytes:
                result.budget_exceeded = True
                break

            chunk = stream.read(_CHUNK)
            if not chunk:
                result.exhausted = True
                break

            decompressed += len(chunk)
            buf = tail + chunk

            for pat in pats:
                m = pat.search(buf)
                if m:
                    result.matched = True
                    result._hit_offset = m.start()
                    _confirm(result, buf, m.start(), stream)
                    result.bytes_scanned = counter.bytes_read
                    result.decompressed_bytes = decompressed
                    result.elapsed_seconds = time.monotonic() - started
                    return result

            tail = buf[-_OVERLAP:]

            if on_progress is not None and (now - last_report) >= 1.0:
                last_report = now
                on_progress(counter.bytes_read, decompressed)

        result.bytes_scanned = counter.bytes_read if counter else 0
        result.decompressed_bytes = decompressed
    except Exception as e:  # noqa: BLE001 - a dead URL is a result, not a crash
        result.error = f"{type(e).__name__}: {e}"
        log.warning("probe failed for %s: %s", location, e)
    finally:
        result.elapsed_seconds = time.monotonic() - started
        if counter is not None:
            try:
                counter.close()
            except Exception:
                pass

    return result


def _nearest(pattern: re.Pattern[bytes], window: bytes, rel: int) -> str:
    """The capture of whichever match of `pattern` sits closest to offset
    `rel`, in either direction. Empty string when there is none."""
    best: str = ""
    best_dist: int | None = None
    for m in pattern.finditer(window):
        dist = 0 if m.start() <= rel <= m.end() else min(
            abs(m.start() - rel), abs(m.end() - rel)
        )
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = m.group(1).decode("utf-8", "replace")
    return best


def _confirm(result: ProbeResult, buf: bytes, hit: int, stream) -> None:
    """Tier 2: pull provider-group context out of the bytes around the hit."""
    start = max(0, hit - _CTX_BEFORE)
    window = buf[start:hit + _CTX_AFTER]

    # If the hit landed near the end of the buffer, pull a little more so the
    # closing brace of the provider group is likely inside the window.
    if len(buf) - hit < _CTX_AFTER:
        try:
            more = stream.read(_CTX_AFTER)
            if more:
                window += more
        except Exception:
            pass

    rel = hit - start

    # `provider_group_id` can sit on either side of the npi list depending on
    # the payer: Cigna emits it first, UHC emits it after the provider_groups
    # array. So take whichever occurrence is physically closest to the hit
    # rather than assuming an order.
    gid = _nearest(_RE_GROUP_ID, window, rel)
    tin = _nearest(_RE_TIN_VALUE, window, rel)

    npi_count = 0
    for m in _RE_NPI_ARRAY.finditer(window):
        if m.start() <= rel <= m.end():
            npi_count = len([x for x in m.group(1).split(b",") if x.strip()])
            break

    result.provider_group_id = gid
    result.matched_tin = tin
    result.npi_count = npi_count
    result.confidence = "strong" if (gid or tin or npi_count) else "weak"

    snippet = window[max(0, rel - 220):rel + 220]
    result.context = snippet.decode("utf-8", "replace").replace("\n", " ").strip()
