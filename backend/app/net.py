"""
HTTP streaming helpers for payer-hosted MRFs.

Payer files are multi-GB and always either gzipped or served with
Content-Encoding: gzip, so everything here is streaming-first: nothing is ever
fully buffered in memory, and every reader reports the number of *compressed*
(wire) bytes consumed so progress and ETA track real network work rather than
the much larger decompressed size.
"""
from __future__ import annotations

import gzip
import io
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("mrf.net")

_UA = "Mozilla/5.0 (compatible; MRF-Dashboard/1.0; +transparency-in-coverage)"
GZIP_MAGIC = b"\x1f\x8b"
_CHUNK = 1024 * 256


class CountingReader(io.RawIOBase):
    """Wraps a stream, counting bytes pulled off the wire. Optionally lets a
    few bytes be pushed back so we can sniff the gzip magic and still hand a
    complete stream downstream."""

    def __init__(self, raw, on_bytes: Callable[[int], None] | None = None):
        self._raw = raw
        self._prefix = b""
        self._count = 0
        self._on_bytes = on_bytes

    def unread(self, data: bytes) -> None:
        self._prefix = data + self._prefix

    @property
    def bytes_read(self) -> int:
        return self._count

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._prefix]
            self._prefix = b""
            while True:
                b = self._raw.read(_CHUNK)
                if not b:
                    break
                self._count += len(b)
                if self._on_bytes:
                    self._on_bytes(self._count)
                parts.append(b)
            return b"".join(parts)

        if self._prefix:
            out = self._prefix[:size]
            self._prefix = self._prefix[size:]
            return out

        b = self._raw.read(size)
        if b:
            self._count += len(b)
            if self._on_bytes:
                self._on_bytes(self._count)
        return b

    def readinto(self, buf) -> int:  # noqa: ANN001
        data = self.read(len(buf))
        n = len(data)
        buf[:n] = data
        return n

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass
        super().close()


def open_url(url: str, *, timeout: float = 60.0, on_bytes=None):
    """Open `url` for streaming reads, transparently gunzipping.

    Returns (json_stream, counting_reader, total_wire_bytes_or_None). Close the
    counting reader when done; the caller owns it.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            # Ask for the raw bytes: we handle gzip ourselves so the byte
            # counter measures real transfer, and so a .gz URL isn't
            # double-decoded by urllib.
            "Accept-Encoding": "identity",
            "Accept": "*/*",
        },
    )
    resp = urllib.request.urlopen(req, timeout=timeout)

    total = resp.headers.get("Content-Length")
    total_bytes = int(total) if total and total.isdigit() else None

    counter = CountingReader(resp, on_bytes=on_bytes)
    head = counter.read(2)
    counter.unread(head)

    if head == GZIP_MAGIC or url.lower().endswith(".gz"):
        stream = gzip.GzipFile(fileobj=counter)
    else:
        stream = counter

    return stream, counter, total_bytes


def download_to(
    url: str,
    dest: Path,
    *,
    timeout: float = 120.0,
    progress: Callable[[int, int | None, float | None], None] | None = None,
    progress_interval: float = 1.0,
) -> int:
    """Stream `url` straight to `dest`, byte-for-byte (no decompression - the
    extraction pipeline reads .json.gz natively). Returns the size written.

    `progress(bytes_done, total_bytes_or_None, rate_bytes_per_sec)` fires at
    most every `progress_interval` seconds.
    """
    from .eta import RateEstimator

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Encoding": "identity", "Accept": "*/*"}
    )

    est = RateEstimator()
    done = 0
    last_report = 0.0

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            total = int(cl) if cl and cl.isdigit() else None

            with open(tmp, "wb") as f:
                while True:
                    buf = resp.read(_CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    est.observe(done)

                    now = time.monotonic()
                    if progress and (now - last_report) >= progress_interval:
                        last_report = now
                        progress(done, total, est.rate)

        tmp.replace(dest)
        if progress:
            progress(done, done, est.rate)
        return done
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def probe_size(url: str, timeout: float = 15.0) -> int | None:
    """Content-Length without downloading the body, or None."""
    from .payers import head

    ok, size, _ = head(url, timeout=timeout)
    return size if ok else None
