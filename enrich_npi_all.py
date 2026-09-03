"""
Enrich an existing TiC rate-extraction CSV (from extract_tic_all.py) with
provider/facility names and addresses, looked up from the free public
NPPES NPI Registry.

Columns added (one entry per NPI unless noted, ';'-joined in NPI order):
    provider_names, organization_names, provider_addresses,
    provider_cities, provider_states,
    provider_zips    -- ZIP exactly as NPPES returns it (usually ZIP+4 as
                        9 digits, no hyphen)
    provider_zip5s   -- the same ZIPs normalized to 5 digits
    primary_zip      -- ROW-LEVEL: the first non-empty 5-digit ZIP on the
                        row, as one plain value to group / join / filter on
                        without splitting a list (for the data team)

This is the "all data" counterpart to extract_tic_all.py:
    - No geography filter. Every row from the input CSV is enriched and
      written to the output, regardless of where the provider is
      located (no Orange County / any-county restriction).
    - Capped at the first MAX_NPIS_PER_ROW (5) NPIs per row, to keep
      runtime manageable on large, un-filtered files. If a row has more
      than 5 NPIs, only the first 5 (in the order they appear in the
      "npis" column) are looked up and written; the rest are skipped
      for that row. Repeated NPIs across rows are cached, so each
      unique NPI still only ever costs one API call no matter how many
      rows reference it.
    - Rows where any NPI equals that row's own TIN (after normalizing
      digits/leading zeros) are treated as bad data and skipped
      entirely -- same corruption check extract_tic_all.py applies at
      extraction time, re-applied here as a second safety net in case
      the input CSV came from somewhere else or was hand-edited.

NOTE ON TRADE-OFF: capping means a row's real match (e.g. a provider in
a specific area, or a specific name you're looking for) could be sitting
in NPI #6+ and never get looked up. This is the same risk the earlier
"sample the first N" version had -- it's accepted here deliberately to
cut runtime, not because it's risk-free. If you need guaranteed full
coverage per row, remove MAX_NPIS_PER_ROW (or raise it) and re-run.

Output is written and flushed to disk one row at a time as the script
runs -- if the process is interrupted partway through, every row
written up to that point is already safely on disk in out_csv.

Usage:
    python enrich_npi_all.py input.csv output_enriched.csv
"""

import sys
import re
import csv
import time
import json
import urllib.request
import urllib.parse

# Some rows have very large npis fields, which can exceed Python's
# default 131,072-character CSV field limit. Raise it here. sys.maxsize can
# overflow the underlying C long on some platforms, so fall back to a large
# fixed value if that happens.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
REQUEST_DELAY_SECONDS = 0.1  # be polite to the free public API

# Only look up the first N NPIs per row -- keeps runtime down on large
# files at the cost of possibly missing a real match beyond NPI #N.
# See NOTE ON TRADE-OFF in the module docstring.
MAX_NPIS_PER_ROW = 5


def normalize_id(raw):
    """Strip everything but digits, then strip leading zeros, so ids
    like '0141536357' and '141536357' compare equal. Used to detect a
    row where an NPI is actually the TIN duplicated into the npi list
    (a data-entry error on the payer's side)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits.lstrip("0") or "0"


def row_npi_list(row):
    """Return this row's npis, capped at MAX_NPIS_PER_ROW, as a list of
    raw (un-normalized) strings -- same splitting logic used both in the
    prescan and the real pass, so their counts line up."""
    npi_field = row.get("npis", "") or ""
    npi_list = [n.strip() for n in npi_field.split(";") if n.strip()]
    return npi_list[:MAX_NPIS_PER_ROW]


def row_is_npi_eq_tin(row, npi_list):
    """True if any NPI in npi_list matches this row's own TIN once both
    are normalized -- i.e. this row is corrupted data and should be
    skipped entirely, not enriched or written."""
    tin_norm = normalize_id(row.get("tin", ""))
    return any(normalize_id(n) == tin_norm for n in npi_list)


EMPTY_NPI_INFO = {"provider_name": "", "organization_name": "", "address": "",
                  "city": "", "state": "", "zip": "", "zip5": ""}


def zip5(raw):
    """First 5 digits of a postal code. NPPES returns ZIP+4 as nine digits
    with no hyphen (e.g. '146043923'); the data team wants the plain
    5-digit ZIP for joins to census / geo tables. Returns '' if there
    aren't 5 digits to take."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[:5] if len(digits) >= 5 else ""


def _fetch_npi(npi):
    """HTTP + parse a single NPI from the NPPES registry, with NO caching or
    dedupe (the caller owns that). Returns a dict with provider_name,
    organization_name, address, city, state, zip. Safe to call from worker
    threads -- it only does a request, a parse, and a short politeness
    sleep, touching no shared state."""
    npi = (npi or "").strip()
    if not npi:
        return dict(EMPTY_NPI_INFO)

    result = dict(EMPTY_NPI_INFO)

    try:
        params = urllib.parse.urlencode({"number": npi, "version": "2.1"})
        url = f"{NPPES_URL}?{params}"
        print(f"  Looking up NPI {npi} via NPPES...", flush=True)
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        if results:
            r = results[0]
            basic = r.get("basic", {})

            # Individual providers have first/last name; organizations
            # (facilities, hospital systems) have organization_name instead.
            if basic.get("organization_name"):
                result["organization_name"] = basic.get("organization_name", "")
            else:
                first = basic.get("first_name", "")
                last = basic.get("last_name", "")
                credential = basic.get("credential", "")
                full_name = " ".join(p for p in [first, last] if p)
                if credential:
                    full_name = f"{full_name}, {credential}"
                result["provider_name"] = full_name

            addresses = r.get("addresses", [])
            practice_addr = next(
                (a for a in addresses if a.get("address_purpose") == "LOCATION"),
                addresses[0] if addresses else None
            )
            if practice_addr:
                line1 = practice_addr.get("address_1", "")
                line2 = practice_addr.get("address_2", "")
                result["address"] = " ".join(p for p in [line1, line2] if p)
                result["city"] = practice_addr.get("city", "")
                result["state"] = practice_addr.get("state", "")
                result["zip"] = practice_addr.get("postal_code", "")
                result["zip5"] = zip5(result["zip"])

    except Exception as e:
        print(f"  Warning: NPI lookup failed for {npi}: {e}")

    time.sleep(REQUEST_DELAY_SECONDS)
    return result


def lookup_npi(npi, cache):
    """Cached single-NPI lookup: same behaviour the CLI enrich path has
    always had -- a repeated NPI costs one API call. Thin wrapper over
    _fetch_npi()."""
    npi = (npi or "").strip()
    if not npi:
        return dict(EMPTY_NPI_INFO)
    if npi in cache:
        return cache[npi]
    result = _fetch_npi(npi)
    cache[npi] = result
    return result


ENRICH_COLUMNS = [
    "provider_names", "organization_names", "provider_addresses",
    "provider_cities", "provider_states",
    # zips: raw = whatever NPPES returned (often ZIP+4, 9 digits, no hyphen);
    # zip5 = normalized 5-digit, one per NPI, ';'-joined; primary_zip = the
    # first non-empty 5-digit ZIP on the row, as a single clean value the
    # data team can group/join/filter on without splitting a list.
    "provider_zips", "provider_zip5s", "primary_zip",
]


def _row_npi_list(row, cap):
    npi_field = row.get("npis", "") or ""
    npis = [n.strip() for n in npi_field.split(";") if n.strip()]
    return npis[:cap] if cap and cap > 0 else npis


def _primary_zip(zip5_values):
    """First non-empty 5-digit ZIP from a list (row-level convenience col)."""
    for z in zip5_values:
        if z:
            return z
    return ""


def iter_enriched_rows(rows, *, threads=8, max_npis_per_row=MAX_NPIS_PER_ROW,
                       chunk_size=400, progress=None, stats=None):
    """Streaming counterpart of enrich_csv(): consume an iterable of row
    dicts (straight from extract_tic_all.iter_rate_rows), attach the six
    provider_* columns, and yield each enriched row.

    NPI lookups are batched per `chunk_size` rows and run across a
    ThreadPoolExecutor of `threads` workers, with an in-memory cache so a
    repeated NPI still costs exactly one API call. Rows where an NPI equals
    the row's own TIN are dropped (same corruption guard as the CLI path).

    `progress`: optional hook, called roughly every 5s with kwargs
    (phase="enrich", rows, unique_npis, skipped, elapsed).
    `stats`: optional dict, updated in place with final counters.
    """
    import concurrent.futures as _cf

    cache: dict[str, dict] = {}
    rows_written = 0
    rows_skipped_npi_eq_tin = 0
    unique_looked_up = 0
    buf: list[tuple[dict, list[str]]] = []
    start = time.time()
    last_heartbeat = start

    def _resolve_and_emit(batch):
        nonlocal unique_looked_up
        need, seen = [], set()
        for _row, npi_list in batch:
            for n in npi_list:
                if n and n not in cache and n not in seen:
                    seen.add(n)
                    need.append(n)
        if need:
            if threads and threads > 1:
                with _cf.ThreadPoolExecutor(max_workers=threads) as ex:
                    for n, info in zip(need, ex.map(_fetch_npi, need)):
                        cache[n] = info
            else:
                for n in need:
                    cache[n] = _fetch_npi(n)
            unique_looked_up += len(need)
        out = []
        for _row, npi_list in batch:
            infos = [cache.get(n, EMPTY_NPI_INFO) for n in npi_list]
            zip5s = [i.get("zip5", "") for i in infos]
            _row["provider_names"] = ";".join(i["provider_name"] for i in infos)
            _row["organization_names"] = ";".join(i["organization_name"] for i in infos)
            _row["provider_addresses"] = ";".join(i["address"] for i in infos)
            _row["provider_cities"] = ";".join(i["city"] for i in infos)
            _row["provider_states"] = ";".join(i["state"] for i in infos)
            _row["provider_zips"] = ";".join(i["zip"] for i in infos)
            _row["provider_zip5s"] = ";".join(zip5s)
            _row["primary_zip"] = _primary_zip(zip5s)
            out.append(_row)
        return out

    for row in rows:
        npi_list = _row_npi_list(row, max_npis_per_row)
        if row_is_npi_eq_tin(row, npi_list):
            rows_skipped_npi_eq_tin += 1
            continue
        buf.append((row, npi_list))
        if len(buf) >= chunk_size:
            for r in _resolve_and_emit(buf):
                rows_written += 1
                yield r
            buf.clear()
            now = time.time()
            if progress is not None and now - last_heartbeat >= 5:
                progress(phase="enrich", rows=rows_written,
                         unique_npis=unique_looked_up,
                         skipped=rows_skipped_npi_eq_tin, elapsed=now - start)
                last_heartbeat = now

    if buf:
        for r in _resolve_and_emit(buf):
            rows_written += 1
            yield r
        buf.clear()

    if progress is not None:
        progress(phase="enrich", rows=rows_written, unique_npis=unique_looked_up,
                 skipped=rows_skipped_npi_eq_tin, elapsed=time.time() - start)
    if stats is not None:
        stats.update(rows_written=rows_written,
                     rows_skipped_npi_eq_tin=rows_skipped_npi_eq_tin,
                     unique_npis=unique_looked_up)


def prescan_csv(in_csv):
    """Quick first pass: count total rows (excluding NPI==TIN rows, which
    will be skipped in the real pass) and total unique NPIs actually worth
    looking up, without doing any API calls. This lets us give an accurate
    ETA based on the real workload, not just file size.
    """
    total_rows = 0
    skip_rows = 0
    unique_npis = set()
    with open(in_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            npi_list = row_npi_list(row)
            if row_is_npi_eq_tin(row, npi_list):
                skip_rows += 1
                continue
            total_rows += 1
            for n in npi_list:
                unique_npis.add(n)
    return total_rows, skip_rows, len(unique_npis)


def enrich_csv(in_csv, out_csv):
    cache = {}
    rows_processed = 0
    rows_written = 0
    rows_skipped_npi_eq_tin = 0
    unique_npis_looked_up = 0

    print("Pre-scanning file to count rows and unique NPIs (no API calls yet)...", flush=True)
    total_rows, prescan_skip_rows, total_unique_npis = prescan_csv(in_csv)
    print(f"Pre-scan complete: {total_rows} rows to enrich "
          f"({prescan_skip_rows} row(s) will be skipped as NPI==TIN), "
          f"{total_unique_npis} unique NPIs to look up.", flush=True)

    lookup_start = time.time()
    last_heartbeat = lookup_start

    with open(in_csv, newline="", encoding="utf-8") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = reader.fieldnames + list(ENRICH_COLUMNS)

        # Open output immediately and write the header right away -- the
        # file exists and is readable from the very start of the run, not
        # only once the whole input has been processed.
        with open(out_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.DictWriter(f_out, fieldnames=fieldnames)
            writer.writeheader()
            f_out.flush()

            for row in reader:
                npi_list = row_npi_list(row)

                # Same corruption check as extract_tic_all.py: if any NPI
                # in this row is actually the TIN duplicated into the npi
                # list, the row is bad data -- skip it entirely, don't
                # look anything up, don't write it.
                if row_is_npi_eq_tin(row, npi_list):
                    rows_skipped_npi_eq_tin += 1
                    continue

                rows_processed += 1

                provider_names = []
                organization_names = []
                addresses = []
                cities = []
                states = []
                zips = []
                zip5s = []

                for npi in npi_list:
                    before = len(cache)
                    info = lookup_npi(npi, cache)
                    if len(cache) > before:
                        unique_npis_looked_up += 1
                    provider_names.append(info["provider_name"])
                    organization_names.append(info["organization_name"])
                    addresses.append(info["address"])
                    cities.append(info["city"])
                    states.append(info["state"])
                    zips.append(info["zip"])
                    zip5s.append(info.get("zip5", ""))

                # No geography filter -- every remaining row is written,
                # regardless of where the resolved provider(s) are located.
                row["provider_names"] = ";".join(provider_names)
                row["organization_names"] = ";".join(organization_names)
                row["provider_addresses"] = ";".join(addresses)
                row["provider_cities"] = ";".join(cities)
                row["provider_states"] = ";".join(states)
                row["provider_zips"] = ";".join(zips)
                row["provider_zip5s"] = ";".join(zip5s)
                row["primary_zip"] = _primary_zip(zip5s)

                # Write this row and flush to disk immediately -- an
                # interrupted run still leaves every row written so far
                # readable in out_csv, instead of losing everything.
                writer.writerow(row)
                f_out.flush()
                rows_written += 1

                # --- Heartbeat / progress / ETA (runs for every row) ------
                now = time.time()
                if now - last_heartbeat >= 5:
                    elapsed = now - lookup_start
                    pct = (100.0 * unique_npis_looked_up / total_unique_npis
                           if total_unique_npis else 100.0)
                    rate = unique_npis_looked_up / elapsed if elapsed > 0 else 0
                    remaining_npis = total_unique_npis - unique_npis_looked_up
                    eta_sec = remaining_npis / rate if rate > 0 else 0
                    print(f"  [{int(elapsed)}s elapsed] {pct:.1f}% of unique NPIs resolved "
                          f"({unique_npis_looked_up}/{total_unique_npis}), "
                          f"{rows_processed}/{total_rows} rows scanned, "
                          f"{rows_written} written, {rows_skipped_npi_eq_tin} skipped (NPI==TIN) — "
                          f"ETA ~{int(eta_sec // 60)}m {int(eta_sec % 60)}s", flush=True)
                    last_heartbeat = now

                if rows_processed % 500 == 0:
                    print(f"  ...processed {rows_processed}/{total_rows} rows, "
                          f"{unique_npis_looked_up}/{total_unique_npis} unique NPIs looked up so far", flush=True)

    print(f"Done. Scanned {rows_processed} row(s), looked up {unique_npis_looked_up} unique NPIs "
          f"(capped at {MAX_NPIS_PER_ROW} per row). Wrote {rows_written} row(s) to {out_csv}. "
          f"Skipped {rows_skipped_npi_eq_tin} row(s) where an NPI matched its own TIN. "
          f"(no geography filter applied)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python enrich_npi_all.py <input.csv> <output_enriched.csv>")
        sys.exit(1)
    enrich_csv(sys.argv[1], sys.argv[2])
