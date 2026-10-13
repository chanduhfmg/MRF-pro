"""
Discover what fields a payer's in-network-rates file actually contains,
without extracting any rate data.

Payers drift from the baseline TiC schema in both directions - some omit
optional fields, some add custom ones (a few append extra pricing metadata,
regional modifiers, etc.). Before running a real extraction job (which can
take a long time on a multi-GB file), this answers "what columns would I
actually get" in a few seconds, by streaming just the first handful of
in-network items from one representative file - the same provider-group
join and rate matching that `extract_tic_all.py` does is skipped entirely;
this only reads field *names*.
"""
from __future__ import annotations

import logging
import sys
import time

import ijson

from . import payers
from .config import REPO_ROOT
from .index_store import get_index_url
from .mrf_index import iter_index_entries, iter_local_index_entries
from .net import open_url
from .payers import local_index_files

log = logging.getLogger("mrf.column_probe")


def _known_columns() -> tuple[set[str], set[str]]:
    """(item-level known fields, price-level known fields), split out of
    extract_tic_all.py's own KNOWN_COLUMNS so "extra" here means exactly
    what would land in a real extraction's `extra_fields` column."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import extract_tic_all as extract_mod  # type: ignore

    # The four synthetic columns the extractor always adds itself - never
    # read from the file, so they're not part of either "what's in the
    # file" set.
    synthetic = {"provider_group_id", "tin", "npis", "npi_count"}
    price_known = {
        "negotiated_type", "negotiated_rate", "billing_class",
        "service_code", "billing_code_modifier", "setting",
        "expiration_date",
    }
    item_known = set(extract_mod.KNOWN_COLUMNS) - synthetic - price_known
    return item_known, price_known


class ColumnProbeFailed(Exception):
    pass


def _pick_candidate_file(company: str, *, max_candidates: int = 5) -> tuple[str, str]:
    """One real in-network file location to sample, plus a human-readable
    label for where its index came from. Prefers locally-provided index
    files (Aetna, MVP - see payers.local_index_files) over the network,
    since a payer with those on disk has them precisely because the network
    host is blocked."""
    local_files = local_index_files(company)
    if local_files:
        entries = list(
            iter_local_index_entries(local_files, max_entries=max_candidates)
        )
        if not entries:
            raise ColumnProbeFailed(
                f"The local index file(s) for {company} list no in-network files."
            )
        return entries[0].location, f"{len(local_files)} local index file(s)"

    src = payers.get_source(company)
    resolved = get_index_url(company)
    if not resolved.get("url"):
        raise ColumnProbeFailed(
            resolved.get("error") or f"no index URL available for {company}"
        )

    entries = list(
        iter_index_entries(
            resolved["url"], max_entries=max_candidates, toc_kind=src.toc_kind
        )
    )
    if not entries:
        raise ColumnProbeFailed(
            f"{company}'s table of contents listed no in-network files."
        )
    return entries[0].location, resolved["url"]


def probe_columns(
    company: str,
    *,
    sample_items: int = 150,
    max_wire_bytes: int = 60 * 1024 * 1024,
    timeout_seconds: float = 45.0,
) -> dict:
    """Stream the first `sample_items` in_network entries of one of
    `company`'s current files (or fewer, whichever budget is hit first) and
    return every distinct field name seen, split into item-level and
    price-level, each further split into the baseline TiC columns and
    whatever this payer adds beyond that."""
    location, index_source = _pick_candidate_file(company)
    item_known, price_known = _known_columns()

    item_fields: set[str] = set()
    price_fields: set[str] = set()
    items_sampled = 0
    exhausted = False
    budget_exceeded = False
    error: str | None = None
    file_size_bytes: int | None = None

    started = time.monotonic()
    stream = counter = None
    try:
        stream, counter, file_size_bytes = open_url(location, timeout=20.0)
        for item in ijson.items(stream, "in_network.item"):
            if items_sampled >= sample_items:
                break
            if counter.bytes_read > max_wire_bytes:
                budget_exceeded = True
                break
            if time.monotonic() - started > timeout_seconds:
                budget_exceeded = True
                break

            item_fields.update(k for k in item.keys() if k != "negotiated_rates")
            for rate_entry in item.get("negotiated_rates", []):
                for price in rate_entry.get("negotiated_prices", []):
                    price_fields.update(price.keys())
            items_sampled += 1
        else:
            exhausted = True
    except Exception as e:  # noqa: BLE001 - report it, don't crash the request
        error = f"{type(e).__name__}: {e}"
        log.warning("column probe failed for %s (%s): %s", company, location, e)
    finally:
        if counter is not None:
            try:
                counter.close()
            except Exception:
                pass

    return {
        "company": company,
        "sampled_file": location,
        "sampled_filename": location.split("?", 1)[0].rsplit("/", 1)[-1],
        "index_source": index_source,
        "file_size_bytes": file_size_bytes,
        "items_sampled": items_sampled,
        "exhausted": exhausted,
        "budget_exceeded": budget_exceeded,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "item_level_columns": sorted(item_fields),
        "item_level_standard_columns": sorted(item_fields & item_known),
        "item_level_extra_columns": sorted(item_fields - item_known),
        "price_level_columns": sorted(price_fields),
        "price_level_standard_columns": sorted(price_fields & price_known),
        "price_level_extra_columns": sorted(price_fields - price_known),
        "error": error,
    }


def probe_all_payers(*, sample_items: int = 100) -> list[dict]:
    """One probe_columns() result per known payer, for the cross-payer
    download - a payer whose host is blocked (Aetna, MVP) or otherwise
    fails still gets a row, with its `error` set, rather than being
    dropped silently."""
    results = []
    for key in payers.PAYER_KEYS:
        try:
            results.append(probe_columns(key, sample_items=sample_items))
        except ColumnProbeFailed as e:
            results.append({
                "company": key,
                "sampled_file": None,
                "sampled_filename": None,
                "index_source": None,
                "file_size_bytes": None,
                "items_sampled": 0,
                "exhausted": False,
                "budget_exceeded": False,
                "elapsed_seconds": 0.0,
                "item_level_columns": [],
                "item_level_standard_columns": [],
                "item_level_extra_columns": [],
                "price_level_columns": [],
                "price_level_standard_columns": [],
                "price_level_extra_columns": [],
                "error": str(e),
            })
    return results
