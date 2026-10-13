"""
Stream a payer Table-of-Contents ("index") file and enumerate every
in-network-rates file it points at.

The CMS TiC schema for a TOC is:

    {
      "reporting_entity_name": ...,
      "reporting_structure": [
        {
          "reporting_plans":  [{plan_name, plan_id, plan_id_type, plan_market_type}, ...],
          "in_network_files": [{description, location}, ...],
          "allowed_amount_file": {description, location}
        },
        ...
      ]
    }

Two things make this hostile to naive parsing, and both are handled here:

1. **Size.** Anthem's TOC is ~10 GB gzipped and repeats the same file
   reference millions of times across plans. So we use `ijson.parse` (event
   level, never materializing the array) and de-duplicate on the `location`
   URL, keeping the first plan name we saw for each file.

2. **Shape drift.** Some payers emit a flat `in_network_files` at the top
   level instead of nesting under `reporting_structure`. The parser keys off
   the prefix *suffix* rather than an exact path, so both shapes work.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import ijson

from .net import open_url

log = logging.getLogger("mrf.index")

# Payers label out-of-network / allowed-amount files in the same array shape;
# these markers in a description or filename mean "not an in-network rates file".
_NOT_IN_NETWORK = re.compile(
    r"(allowed[-_ ]?amount|out[-_ ]?of[-_ ]?network|provider[-_ ]?reference)", re.I
)


@dataclass
class IndexEntry:
    """One in-network-rates file advertised by a payer's TOC."""

    location: str
    description: str = ""
    plan_name: str = ""
    plan_id: str = ""
    plan_market_type: str = ""
    size_bytes: int | None = None
    # how many distinct reporting_structure blocks referenced this same file
    reference_count: int = 1

    @property
    def filename(self) -> str:
        path = self.location.split("?", 1)[0]
        return path.rsplit("/", 1)[-1] or path

    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "filename": self.filename,
            "description": self.description,
            "plan_name": self.plan_name,
            "plan_id": self.plan_id,
            "plan_market_type": self.plan_market_type,
            "size_bytes": self.size_bytes,
            "reference_count": self.reference_count,
        }


@dataclass
class _Block:
    """A reporting_structure item being assembled."""

    plan_name: str = ""
    plan_id: str = ""
    plan_market_type: str = ""
    files: list[dict] = field(default_factory=list)
    cur_file: dict | None = None


def iter_index_entries(
    index_url: str,
    *,
    max_entries: int | None = None,
    progress: Callable[..., None] | None = None,
    timeout: float = 120.0,
    heartbeat_seconds: float = 3.0,
    toc_kind: str = "cms_index",
) -> Iterator[IndexEntry]:
    """Yield unique in-network file entries from the index at `index_url`.

    `toc_kind` selects the reader - see PayerSource.toc_kind. `progress` is
    called about every `heartbeat_seconds` with kwargs (bytes_read,
    total_bytes, entries, blocks, elapsed).
    """
    if toc_kind == "uhc_blobs":
        yield from _iter_uhc_blobs(
            index_url,
            max_entries=max_entries,
            progress=progress,
            timeout=timeout,
            heartbeat_seconds=heartbeat_seconds,
        )
        return
    yield from _iter_cms_index(
        index_url,
        max_entries=max_entries,
        progress=progress,
        timeout=timeout,
        heartbeat_seconds=heartbeat_seconds,
    )


def iter_local_index_entries(
    paths: list[Path],
    *,
    max_entries: int | None = None,
    progress: Callable[..., None] | None = None,
    heartbeat_seconds: float = 3.0,
) -> Iterator[IndexEntry]:
    """Same idea as `iter_index_entries`, but reads one or more locally-saved
    index files off disk instead of fetching a payer-wide index over the
    network - see payers.local_index_files. Each file is either the standard
    CMS reporting_structure TOC shape, or a flat JSON array of entries (see
    `_iter_local_file`); entries are deduplicated by location across every
    file, same as within one.
    """
    seen: set[str] = set()
    n = 0
    for path in paths:
        remaining = (max_entries - n) if max_entries else None
        if remaining is not None and remaining <= 0:
            break
        for entry in _iter_local_file(
            path,
            max_entries=remaining,
            progress=progress,
            heartbeat_seconds=heartbeat_seconds,
        ):
            if entry.location in seen:
                continue
            seen.add(entry.location)
            n += 1
            yield entry


def _iter_local_file(
    path: Path,
    *,
    max_entries: int | None,
    progress: Callable[..., None] | None,
    heartbeat_seconds: float,
) -> Iterator[IndexEntry]:
    """One locally-saved file, in whichever of two shapes it turns out to be:

      - the standard CMS TOC (a JSON *object* with reporting_structure[]),
        read the normal streaming way through `_iter_cms_index`; or
      - a flat JSON *array* of {location, description, plan_name/plans, ...}
        - e.g. a TOC someone (or this app) already flattened by hand into
        one list across several source files.

    Sniffed from the first non-whitespace byte after ungzipping if needed:
    '[' is the flat shape, '{' is a TOC.
    """
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    stripped = raw.lstrip()
    if stripped[:1] == b"[":
        yield from _iter_flat_entries(stripped, max_entries=max_entries)
        return
    yield from _iter_cms_index(
        path.as_uri(),
        max_entries=max_entries,
        progress=progress,
        heartbeat_seconds=heartbeat_seconds,
    )


def _iter_flat_entries(raw: bytes, *, max_entries: int | None) -> Iterator[IndexEntry]:
    items = json.loads(raw)
    if not isinstance(items, list):
        return
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        loc = str(item.get("location") or "").strip()
        if not loc or not loc.lower().startswith(("http://", "https://")):
            continue
        desc = str(item.get("description") or "").strip()
        if _NOT_IN_NETWORK.search(desc) or _NOT_IN_NETWORK.search(loc):
            continue
        if loc.split("?", 1)[0].lower().endswith(".zip"):
            continue
        plans = item.get("plans")
        plan_name = item.get("plan_name") or (plans[0] if isinstance(plans, list) and plans else "") or ""
        size = item.get("size_bytes")
        yield IndexEntry(
            location=loc,
            description=desc,
            plan_name=str(plan_name),
            plan_id=str(item.get("plan_id") or ""),
            plan_market_type=str(item.get("plan_market_type") or ""),
            size_bytes=int(size) if isinstance(size, (int, float)) else None,
        )
        n += 1
        if max_entries and n >= max_entries:
            return


# --- UHC: a flat blob listing, not a TiC table of contents ------------------

_UHC_IN_NETWORK_SUFFIX = "_in-network-rates.json.gz"


def _iter_uhc_blobs(
    index_url: str,
    *,
    max_entries: int | None,
    progress: Callable[..., None] | None,
    timeout: float,
    heartbeat_seconds: float,
) -> Iterator[IndexEntry]:
    """UHC publishes one flat list of every file for the reporting period:

        {"blobs": [{"name": ..., "downloadUrl": ..., "size": 38209124}, ...]}

    Tens of thousands of entries, most of them per-employer `_index.json`
    stubs and allowed-amount files. We keep only the in-network-rates files -
    those are the ones that actually contain provider groups and rates. The
    inline `size` is a real byte count, so ETAs for UHC are exact rather than
    estimated.
    """
    import time

    started = time.monotonic()
    last_beat = started
    count = 0
    scanned = 0

    stream, counter, total_bytes = open_url(index_url, timeout=timeout)
    try:
        for blob in ijson.items(stream, "blobs.item"):
            scanned += 1
            now = time.monotonic()
            if progress is not None and (now - last_beat) >= heartbeat_seconds:
                last_beat = now
                progress(
                    bytes_read=counter.bytes_read,
                    total_bytes=total_bytes,
                    entries=count,
                    blocks=scanned,
                    elapsed=now - started,
                )

            name = str(blob.get("name") or "")
            if not name.endswith(_UHC_IN_NETWORK_SUFFIX):
                continue
            loc = str(blob.get("downloadUrl") or "")
            if not loc.lower().startswith(("http://", "https://")):
                continue

            size = blob.get("size")
            count += 1
            yield IndexEntry(
                location=loc,
                description=name,
                plan_name=_uhc_plan_name(name),
                size_bytes=int(size) if isinstance(size, (int, float)) else None,
            )
            if max_entries and count >= max_entries:
                return
    finally:
        if progress is not None:
            progress(
                bytes_read=counter.bytes_read,
                total_bytes=total_bytes,
                entries=count,
                blocks=scanned,
                elapsed=time.monotonic() - started,
                done=True,
            )
        try:
            counter.close()
        except Exception:
            pass


def _uhc_plan_name(blob_name: str) -> str:
    """'2026-09-01_Golden-Rule_Insurer_PPO_in-network-rates.json.gz'
    -> 'Golden Rule Insurer PPO'."""
    stem = blob_name[: -len(_UHC_IN_NETWORK_SUFFIX)] if blob_name.endswith(
        _UHC_IN_NETWORK_SUFFIX
    ) else blob_name
    parts = stem.split("_")
    if parts and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
        parts = parts[1:]
    return " ".join(p.replace("-", " ").strip() for p in parts if p).strip()


def _iter_cms_index(
    index_url: str,
    *,
    max_entries: int | None = None,
    progress: Callable[..., None] | None = None,
    timeout: float = 120.0,
    heartbeat_seconds: float = 3.0,
) -> Iterator[IndexEntry]:
    """The standard TiC table of contents (reporting_structure[])."""
    import time

    seen: dict[str, IndexEntry] = {}
    blocks = 0
    started = time.monotonic()
    last_beat = started

    stream, counter, total_bytes = open_url(index_url, timeout=timeout)
    try:
        block = _Block()
        for prefix, event, value in ijson.parse(stream):
            now = time.monotonic()
            if progress is not None and (now - last_beat) >= heartbeat_seconds:
                last_beat = now
                progress(
                    bytes_read=counter.bytes_read,
                    total_bytes=total_bytes,
                    entries=len(seen),
                    blocks=blocks,
                    elapsed=now - started,
                )

            # --- reporting_structure item boundaries ---
            if prefix.endswith("reporting_structure.item") and event == "start_map":
                block = _Block()
                continue
            if prefix.endswith("reporting_structure.item") and event == "end_map":
                blocks += 1
                for f in block.files:
                    entry = _finish(f, block)
                    if entry is None:
                        continue
                    prior = seen.get(entry.location)
                    if prior is None:
                        seen[entry.location] = entry
                        yield entry
                        if max_entries and len(seen) >= max_entries:
                            return
                    else:
                        prior.reference_count += 1
                block = _Block()
                continue

            # --- plan metadata (first plan in the block wins) ---
            if prefix.endswith("reporting_plans.item.plan_name") and not block.plan_name:
                block.plan_name = str(value or "")
            elif prefix.endswith("reporting_plans.item.plan_id") and not block.plan_id:
                block.plan_id = str(value or "")
            elif (
                prefix.endswith("reporting_plans.item.plan_market_type")
                and not block.plan_market_type
            ):
                block.plan_market_type = str(value or "")

            # --- in_network_files entries ---
            elif prefix.endswith("in_network_files.item") and event == "start_map":
                block.cur_file = {}
            elif prefix.endswith("in_network_files.item") and event == "end_map":
                if block.cur_file:
                    block.files.append(block.cur_file)
                block.cur_file = None
            elif prefix.endswith("in_network_files.item.location"):
                if block.cur_file is not None:
                    block.cur_file["location"] = str(value or "")
            elif prefix.endswith("in_network_files.item.description"):
                if block.cur_file is not None:
                    block.cur_file["description"] = str(value or "")

        # A flat/odd-shaped TOC may never close a reporting_structure item.
        for f in block.files:
            entry = _finish(f, block)
            if entry is not None and entry.location not in seen:
                seen[entry.location] = entry
                yield entry
    finally:
        if progress is not None:
            progress(
                bytes_read=counter.bytes_read,
                total_bytes=total_bytes,
                entries=len(seen),
                blocks=blocks,
                elapsed=time.monotonic() - started,
                done=True,
            )
        try:
            counter.close()
        except Exception:
            pass


def _finish(raw: dict, block: _Block) -> IndexEntry | None:
    loc = (raw.get("location") or "").strip()
    if not loc or not loc.lower().startswith(("http://", "https://")):
        return None
    desc = (raw.get("description") or "").strip()
    if _NOT_IN_NETWORK.search(desc) or _NOT_IN_NETWORK.search(loc):
        return None
    # Cigna's TOC includes a few partner networks published as .zip on other
    # hosts. The CMS schema calls for .json/.json.gz; a zip's deflate stream
    # can't be byte-scanned or ijson-parsed, so listing it would only ever
    # produce an "unreachable"/"not JSON" row. Leave them out.
    if loc.split("?", 1)[0].lower().endswith(".zip"):
        return None
    return IndexEntry(
        location=loc,
        description=desc,
        plan_name=block.plan_name,
        plan_id=block.plan_id,
        plan_market_type=block.plan_market_type,
    )


# Below this, a payer "in-network" file is a stub - a header and an empty or
# near-empty array (UHC publishes thousands of ~1 KB carve-out placeholders).
# They cost a request each and can never contain a provider, so they go last.
MIN_USEFUL_BYTES = 1_000_000


def rank_entries(
    entries: list[IndexEntry],
    hint: str = "",
    *,
    budget_bytes: int | None = None,
    min_useful_bytes: int = MIN_USEFUL_BYTES,
) -> list[IndexEntry]:
    """Order candidates so the scan budget is spent where a hit is plausible.

    There is no way to know which file holds a given NPI without looking, so
    ranking is about expected value per probe. Four tiers, best first:

      0. Substantial *and* fully scannable within the probe budget. Within
         this tier, bigger is better: a larger file is a broader network and
         so more likely to contain any given provider.
      1. Size unknown - worth a look, we just can't reason about it.
      2. Bigger than the budget. Still worth probing (a hit can come early,
         and the scan aborts the moment it lands), but only partially
         conclusive, so smaller-over-budget first.
      3. Stub files under `min_useful_bytes`. Effectively empty - probed only
         if nothing better is left.

    A caller-supplied `hint` (a state, an employer, a plan name) outranks all
    of it, and only reorders - it never excludes a file.
    """
    hint_l = hint.strip().lower()

    def score(e: IndexEntry) -> tuple:
        hit = 0
        if hint_l:
            hay = f"{e.description} {e.plan_name} {e.filename}".lower()
            hit = 1 if hint_l in hay else 0

        size = e.size_bytes
        if size is None or size <= 0:
            tier, within = 1, 0
        elif size < min_useful_bytes:
            tier, within = 3, -size
        elif budget_bytes and size > budget_bytes:
            tier, within = 2, size
        else:
            tier, within = 0, -size

        return (-hit, tier, -e.reference_count, within, e.filename)

    return sorted(entries, key=score)
