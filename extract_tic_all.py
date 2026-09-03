"""
Extract E&M + ophthalmic negotiated rates from a Transparency-in-Coverage
(TiC) machine-readable file (in-network rates), streamed directly from
.json.gz -- for ALL provider groups / TINs in the file (not narrowed
down to one target provider or one target TIN). Built for large payer
files like Anthem/Cigna where you want the full rate landscape across
every billing code we care about, not one practice's rates.

Filters applied:
    1. billing_code in TARGET_CODES (the FULL combined list: the
       original E&M code set + the ophthalmic/eye-exam code set --
       nothing is TIN-restricted; every provider_groups sub-group found
       in the file is captured for any of these codes, checked in a
       single combined pass -- not two separate scans).
    2. Any individual NPI that is identical to its own sub-group's TIN
       (after normalization) is dropped as bad data -- see note below.
       No TIN-targeting filter is applied beyond that.

--------------------------------------------------------------------------
Why an NPI can equal a TIN in the raw file, and why the whole row is
skipped when it happens:
    A provider_reference's "provider_groups" array occasionally has a
    sub-group where the tax-id value has been duplicated into the npi
    list -- a data-entry error on the payer's side, not a real 10-digit
    NPI. A legitimate NPI and a legitimate TIN identify two different
    things, so if ANY NPI in a sub-group matches that sub-group's own
    TIN (after stripping non-digits and leading zeros), the ENTIRE
    sub-group is treated as corrupted and skipped -- no rows are
    written for it at all, even for its other, unaffected NPIs. We
    don't try to salvage the "clean" NPIs out of a sub-group that's
    already shown itself to be bad data.

Row shape / no cross-TIN bundling:
    A single provider_reference's "provider_groups" array can bundle
    many unrelated TINs under one provider_group_id (e.g. a whole payer
    network registered under one reference id). To avoid the earlier
    bug where NPIs from different TINs got merged into a single row,
    this script writes ONE ROW PER (price entry x provider sub-group),
    so a row's "npis" column only ever contains NPIs belonging to that
    same row's single "tin" value -- never mixed with another TIN's
    roster.

    Because there is no TIN filter, this will produce a LOT of rows on
    a full payer file (every provider group billing any target code).
    That's expected and intentional for this "all data" version.

Output:
    Writes results to CSV, one row at a time, flushing after every
    row. This is a deliberate change from the earlier .xlsx version:
    an Excel file's central directory is only written when the
    workbook is saved, so if the process is killed, crashes, or is
    interrupted partway through, NOTHING is recoverable from a
    partially-built .xlsx. CSV has no such requirement -- every row
    flushed to disk is safely on disk, so an interrupted run still
    leaves you with all rows extracted up to that point. CSV also has
    no 1,048,576-row-per-sheet ceiling, which the old .xlsx version
    would silently loop on forever once crossed (a bug -- the `break`
    only exited the innermost loop, not the whole scan).

Usage:
    pip install ijson --break-system-packages
    python extract_tic_all.py path/to/file.json.gz output.csv
"""

import sys
import os
import re
import gzip
import json
import time
import csv
import ijson
import contextlib

GZIP_MAGIC = b"\x1f\x8b"


def open_json_stream(path):
    """Open `path` for streaming JSON, auto-detecting whether it's
    gzip-compressed or plain JSON (peeks the first 2 bytes for the
    gzip magic number). Returns (raw, stream_cm):
      - raw: the underlying file object -- used for raw.tell() based
        progress/ETA reporting, and must be closed by the caller.
      - stream_cm: a context manager yielding the object to hand to
        ijson (a GzipFile if compressed, otherwise `raw` itself via
        contextlib.nullcontext so it isn't closed twice).
    """
    raw = open(path, "rb")
    magic = raw.peek(2)[:2]
    if magic == GZIP_MAGIC:
        return raw, gzip.GzipFile(fileobj=raw)
    return raw, contextlib.nullcontext(raw)

# --- Codes of interest -------------------------------------------------

# E&M codes -- office/outpatient, hospital, ED, nursing facility, home,
# and preventive visits.
EM_CODES = {
    # New / established patient office or other outpatient visits
    "99202", "99203", "99204", "99205",
    "99211", "99212", "99213", "99214", "99215",
    # Hospital inpatient / observation
    "99221", "99222", "99223",
    "99231", "99232", "99233",
    "99234", "99235", "99236",
    "99238", "99239",
    # Emergency department
    "99281", "99282", "99283", "99284", "99285",
    "99291", "99292", "99293", "99294", "99295",
    # Nursing facility
    "99304", "99305", "99306",
    "99307", "99308", "99309", "99310",
    # Home / residence visits
    "99341", "99342", "99344", "99345",
    "99347", "99348", "99349", "99350",
    # Preventive visits
    "99381", "99382", "99383", "99384", "99385", "99386", "99387",
    "99391", "99392", "99393", "99394", "99395", "99396", "99397",
}

# Ophthalmic / eye-exam codes -- plus a handful of E&M codes that overlap
# with the earlier ophthalmology-focused pass.
OPHTHALMIC_CODES = {
    "99215", "99214", "99213", "99212", "99204", "99203",
    "92250", "92136", "92134", "92133", "92083", "92082",
    "92060", "92025", "92020", "92014", "92012", "92004",
    "76514", "68801", "68761", "67840", "67800", "66984",
    "66821", "66761", "65222", "65220",
}

# --- FULL combined code list (in effect) --------------------------------
# Everything: E&M + ophthalmic, checked together in ONE pass over the
# file. No TIN restriction is applied anywhere below, so this scans
# every provider group in the file for a match on any code in this set.
TARGET_CODES = EM_CODES | OPHTHALMIC_CODES


def normalize_id(raw):
    """Strip everything but digits, then strip leading zeros, so ids
    like '0141536357' and '141536357' compare equal. Used for both NPI
    and TIN comparisons (that's what lets us detect npi == tin)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits.lstrip("0") or "0"


# Known/expected columns, kept in this order. Any field found in the
# source JSON that ISN'T in this list is bundled into "extra_fields" as
# JSON so the CSV header stays fixed (CSV, unlike the old streaming
# Excel writer, needs a stable header written before the first row).
KNOWN_COLUMNS = [
    "billing_code", "billing_code_type", "billing_code_type_version",
    "name", "description", "negotiation_arrangement", "severity_of_illness",
    "negotiated_type", "negotiated_rate", "billing_class",
    "service_code", "billing_code_modifier", "setting", "expiration_date",
    "provider_group_id", "tin", "npis", "npi_count",
]
CSV_FIELDNAMES = KNOWN_COLUMNS + ["extra_fields"]


def _parse_groups(groups):
    """Parse a provider_groups array (from an externally-resolved
    'location' file) into a list of {'tin': value, 'npis': [...]},
    keeping each sub-group's npi list tied to its own TIN."""
    parsed = []
    for g in groups:
        tin = g.get("tin", {})
        parsed.append({
            "tin": tin.get("value"),
            "npis": g.get("npi", []),
        })
    return parsed


# Safety cap per TIN sub-group, applied while streaming. Bounds
# memory/time on pathological files; almost never hit in practice.
MAX_NPIS_PER_TIN_GROUP = 20000


def build_provider_lookup(gz_path, progress=None):
    """First pass: map provider_group_id -> list of sub-groups, where each
    sub-group is {"tin": tin_value, "npis": [...]}.

    Uses a low-level, event-by-event parser (ijson.parse) so we never
    have to materialize the full (potentially huge) provider_references
    array in memory at once.

    Some large networks use a 'pointer' form instead of inline data:
        {"provider_group_id": 166, "location": "https://.../ref_166.json"}
    In that case we fetch the external file to resolve the real
    provider_groups.

    `progress`, if given, is called on each ~5s heartbeat with kwargs
    (phase="provider_lookup", bytes_read, total_bytes, groups, elapsed).
    It's purely a reporting hook -- the CLI path leaves it None and the
    behaviour is unchanged.
    """
    lookup = {}
    pending_urls = {}
    total_size = os.path.getsize(gz_path)

    raw, stream_cm = open_json_stream(gz_path)
    with stream_cm as f:
        parser = ijson.parse(f)
        in_target_array = False
        cur = None          # current provider_reference being built
        cur_group = None    # current provider_groups[i] sub-group being built
        count = 0
        last_heartbeat = time.time()
        start = last_heartbeat
        last_bytes = 0

        for prefix, event, value in parser:
            now = time.time()
            if now - last_heartbeat >= 5:
                bytes_read = raw.tell()
                pct = 100.0 * bytes_read / total_size
                window_secs = now - last_heartbeat
                window_bytes = bytes_read - last_bytes
                rate = window_bytes / window_secs if window_secs > 0 else 0
                eta_sec = (total_size - bytes_read) / rate if rate > 0 else 0
                print(f"  [{int(now - start)}s elapsed] provider_references pass: {pct:.1f}% of file read "
                      f"({count} groups so far) — ETA ~{int(eta_sec // 60)}m {int(eta_sec % 60)}s", flush=True)
                if progress is not None:
                    progress(phase="provider_lookup", bytes_read=bytes_read,
                             total_bytes=total_size, groups=count,
                             elapsed=now - start)
                last_heartbeat = now
                last_bytes = bytes_read

            if prefix == "provider_references" and event == "start_array":
                in_target_array = True
                continue
            if not in_target_array:
                continue
            if prefix == "provider_references" and event == "end_array":
                break

            if prefix == "provider_references.item" and event == "start_map":
                cur = {"gid": None, "location": None, "groups": []}
                continue

            if prefix == "provider_references.item" and event == "end_map":
                count += 1
                if count % 2000 == 0:
                    print(f"  ...scanned {count} provider_references so far", flush=True)
                gid = cur["gid"]
                if cur["location"]:
                    pending_urls[gid] = cur["location"]
                else:
                    lookup[gid] = cur["groups"]
                cur = None
                continue

            if cur is None:
                continue

            if prefix == "provider_references.item.provider_groups.item" and event == "start_map":
                cur_group = {"tin": None, "npis": []}
                continue
            if prefix == "provider_references.item.provider_groups.item" and event == "end_map":
                if cur_group is not None:
                    cur["groups"].append(cur_group)
                cur_group = None
                continue

            if prefix == "provider_references.item.provider_group_id":
                cur["gid"] = value
            elif prefix == "provider_references.item.location":
                cur["location"] = value
            elif prefix.endswith(".provider_groups.item.npi.item"):
                if cur_group is not None and len(cur_group["npis"]) < MAX_NPIS_PER_TIN_GROUP:
                    cur_group["npis"].append(value)
            elif prefix.endswith(".provider_groups.item.tin.value"):
                if cur_group is not None:
                    cur_group["tin"] = value
    raw.close()

    if pending_urls:
        import urllib.request
        print(f"Resolving {len(pending_urls)} external provider-reference files...")
        for gid, url in pending_urls.items():
            try:
                with urllib.request.urlopen(url) as resp:
                    groups = list(ijson.items(resp, "provider_groups.item"))
                lookup[gid] = _parse_groups(groups)
            except Exception as e:
                print(f"  Failed to resolve provider_group_id {gid}: {e}")
                lookup[gid] = []

    return lookup


def iter_rate_rows(gz_path, provider_lookup=None, progress=None, stats=None):
    """Second pass, as a generator: stream `in_network.item`, and for every
    (price entry x provider sub-group) that matches a TARGET_CODE, yield a
    fully-stringified `row` dict ready to hand to csv.DictWriter with
    fieldnames=CSV_FIELDNAMES.

    This is the reusable core of extract_rates(): the CLI wrapper below
    just writes each yielded row to one CSV; the dashboard worker feeds
    the same rows through NPI enrichment and a size-splitting CSV writer.

    - `provider_lookup`: pass a prebuilt lookup (from build_provider_lookup)
      to avoid a second first-pass scan; None => build it here.
    - `progress`: optional hook, called on each ~5s heartbeat with kwargs
      (phase="in_network", bytes_read, total_bytes, items, rows,
      matched_codes, elapsed).
    - `stats`: optional dict, updated in place with final counters
      (items_scanned, rows, dropped_npi_eq_tin, matched_codes,
      extra_columns) so the caller can print a summary.
    """
    if provider_lookup is None:
        provider_lookup = build_provider_lookup(gz_path, progress=progress)

    extra_columns = set()
    dropped_npi_eq_tin = 0
    row_count = 0
    total_size = os.path.getsize(gz_path)
    raw, stream_cm = open_json_stream(gz_path)
    count = 0
    matched_codes = set()
    try:
        with stream_cm as f:
            last_heartbeat = time.time()
            start = last_heartbeat
            last_bytes = 0

            for item in ijson.items(f, "in_network.item"):
                count += 1
                now = time.time()
                if now - last_heartbeat >= 5:
                    bytes_read = raw.tell()
                    pct = 100.0 * bytes_read / total_size
                    window_secs = now - last_heartbeat
                    window_bytes = bytes_read - last_bytes
                    rate = window_bytes / window_secs if window_secs > 0 else 0
                    eta_sec = (total_size - bytes_read) / rate if rate > 0 else 0
                    print(f"  [{int(now - start)}s elapsed] in_network pass: {pct:.1f}% of file read "
                          f"({count} items scanned, {len(matched_codes)}/{len(TARGET_CODES)} unique CPT codes matched, "
                          f"{row_count} rows written) "
                          f"— ETA ~{int(eta_sec // 60)}m {int(eta_sec % 60)}s", flush=True)
                    if progress is not None:
                        progress(phase="in_network", bytes_read=bytes_read,
                                 total_bytes=total_size, items=count, rows=row_count,
                                 matched_codes=len(matched_codes), elapsed=now - start)
                    last_heartbeat = now
                    last_bytes = bytes_read
                if count % 5000 == 0:
                    print(f"  ...scanned {count} in_network items, {row_count} rows written so far", flush=True)

                code = item.get("billing_code")
                if code not in TARGET_CODES:
                    continue
                matched_codes.add(code)

                # Capture every item-level field automatically (except the
                # nested negotiated_rates array) so nothing in the source
                # schema is silently dropped.
                item_fields = {k: v for k, v in item.items() if k != "negotiated_rates"}

                for rate_entry in item.get("negotiated_rates", []):
                    for gid in rate_entry.get("provider_references", []):
                        for g in provider_lookup.get(gid, []):
                            tin_val = g.get("tin")
                            tin_norm = normalize_id(tin_val or "")
                            raw_npis = g.get("npis", [])

                            # If ANY NPI in this sub-group is identical to
                            # its own TIN, the whole sub-group is bad data
                            # -- skip it entirely rather than trying to
                            # salvage the other NPIs out of it.
                            if any(normalize_id(str(n)) == tin_norm for n in raw_npis):
                                dropped_npi_eq_tin += 1
                                continue
                            if not raw_npis:
                                continue  # nothing to report for this sub-group

                            for price in rate_entry.get("negotiated_prices", []):
                                row = {}
                                row.update(item_fields)
                                row.update(price)
                                row["provider_group_id"] = gid
                                row["tin"] = tin_val
                                row["npis"] = ";".join(str(n) for n in raw_npis)
                                row["npi_count"] = len(raw_npis)

                                for k, v in list(row.items()):
                                    if isinstance(v, list):
                                        row[k] = ";".join(str(x) for x in v)
                                    elif isinstance(v, dict):
                                        row[k] = json.dumps(v)

                                extras = {k: v for k, v in row.items() if k not in KNOWN_COLUMNS}
                                if extras:
                                    extra_columns.update(extras.keys())
                                    row["extra_fields"] = json.dumps(extras)

                                row_count += 1
                                if row_count % 10000 == 0:
                                    print(f"NOTE: {row_count:,} rows written so far.", flush=True)
                                yield row
    finally:
        raw.close()
        if stats is not None:
            stats.update(
                items_scanned=count,
                rows=row_count,
                dropped_npi_eq_tin=dropped_npi_eq_tin,
                matched_codes=sorted(matched_codes),
                extra_columns=sorted(extra_columns),
            )


def extract_rates(gz_path, out_csv):
    print(f"Scanning for {len(TARGET_CODES)} target codes "
          f"({len(EM_CODES)} E&M + {len(OPHTHALMIC_CODES)} ophthalmic, "
          f"{len(EM_CODES & OPHTHALMIC_CODES)} overlapping)")

    provider_lookup = build_provider_lookup(gz_path)
    print(f"Loaded {len(provider_lookup)} provider groups")

    # Open the CSV once, write the header immediately, and write +
    # flush every row as it's produced. If the process dies at any
    # point after this, everything written and flushed so far is
    # already safely on disk in out_csv.
    out_file = open(out_csv, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_file, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()
    out_file.flush()

    stats = {}
    try:
        for row in iter_rate_rows(gz_path, provider_lookup=provider_lookup, stats=stats):
            writer.writerow({col: row.get(col, "") for col in CSV_FIELDNAMES})
            out_file.flush()
    finally:
        out_file.close()

    print(f"Done. Scanned {stats.get('items_scanned', 0)} in_network items, "
          f"{len(stats.get('matched_codes', []))}/{len(TARGET_CODES)} unique CPT codes matched "
          f"({stats.get('matched_codes', [])}), wrote {stats.get('rows', 0)} rows to {out_csv}. "
          f"Skipped {stats.get('dropped_npi_eq_tin', 0)} provider sub-group(s) where an NPI "
          f"matched its own TIN.")
    if stats.get("extra_columns"):
        print(f"Note: found {len(stats['extra_columns'])} field(s) beyond the known schema, "
              f"stored as JSON in the 'extra_fields' column: {stats['extra_columns']}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python extract_tic_all.py <input.json.gz> <output.csv>")
        sys.exit(1)
    extract_rates(sys.argv[1], sys.argv[2])
