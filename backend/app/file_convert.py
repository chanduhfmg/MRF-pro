"""
Generic file-format conversion for the "Convert File" sidebar tool: upload
any JSON / JSON.GZ / CSV file, pick an output format (CSV, Excel, or JSON),
get a converted file back.

This is deliberately NOT the MRF extraction pipeline (extract_tic_all.py) -
that one understands the specific Transparency-in-Coverage schema and
streams multi-GB files without ever holding them in memory. This tool is
the opposite: it knows nothing about what shape the JSON is in, so it
flattens whatever it's given generically (the same convention
extract_tic_all.py uses for its own "extra" fields - nested dicts become
dotted keys, lists of scalars join with ';', anything else becomes a JSON
string), and it's capped well below MRF file sizes because it isn't
streaming - see MAX_CONVERT_BYTES.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from pathlib import Path
from typing import Any

# A one-off conversion tool, not the extraction pipeline - keep it well
# under what would require real streaming.
MAX_CONVERT_BYTES = 150 * 1024 * 1024
# Excel's real row cap (including the header row).
XLSX_MAX_ROWS = 1_048_576


class ConvertError(Exception):
    """A problem with the file itself, not the server - reported to the
    user as-is rather than as a 500."""


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":  # gzip magic - trust this over the extension
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", errors="replace")


def _flatten(value: Any, prefix: str = "") -> dict:
    """One JSON object -> one flat row. Nested dict -> dotted keys; a list
    of plain scalars -> ';'-joined; anything else nested (lists of dicts,
    mixed lists) -> a JSON string, so no data is silently dropped even
    when it doesn't fit a flat cell."""
    row: dict = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                row.update(_flatten(v, key))
            elif isinstance(v, list):
                if all(not isinstance(x, (dict, list)) for x in v):
                    row[key] = ";".join("" if x is None else str(x) for x in v)
                else:
                    row[key] = json.dumps(v)
            else:
                row[key] = v
    else:
        row[prefix or "value"] = value
    return row


def json_to_rows(data: Any) -> list[dict]:
    """Picks the most sensible list of "records" out of arbitrary JSON: a
    top-level array, or the first array found inside a top-level object
    (the common {"items": [...]} / {"data": [...]} API shape) - otherwise
    the whole thing becomes one row."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        array_fields = [v for v in data.values() if isinstance(v, list)]
        items = array_fields[0] if array_fields else [data]
    else:
        items = [data]

    return [
        _flatten(item) if isinstance(item, dict) else {"value": item}
        for item in items
    ]


def _fieldnames(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                names.append(k)
    return names


def rows_to_csv(rows: list[dict]) -> bytes:
    fieldnames = _fieldnames(rows)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def rows_to_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    if len(rows) + 1 > XLSX_MAX_ROWS:
        raise ConvertError(
            f"{len(rows):,} rows is over Excel's {XLSX_MAX_ROWS:,}-row limit - "
            "convert to CSV instead."
        )
    fieldnames = _fieldnames(rows)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("data")
    ws.append(fieldnames)
    for row in rows:
        ws.append([row.get(f, "") for f in fieldnames])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_to_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _strip_known_suffixes(filename: str) -> str:
    stem = filename
    for suf in (".json.gz", ".csv.gz", ".gz", ".json", ".csv", ".txt"):
        if stem.lower().endswith(suf):
            return stem[: -len(suf)]
    return stem


def convert_file(src_path: Path, filename: str, target_format: str) -> tuple[bytes, str]:
    """(output_bytes, output_filename). `target_format` is one of
    'csv' | 'xlsx' | 'json'."""
    lower = filename.lower()
    stem = _strip_known_suffixes(filename)
    is_csv_input = lower.endswith(".csv") or lower.endswith(".csv.gz")

    if is_csv_input:
        data: Any = None
        rows = csv_to_rows(_read_text(src_path))
    else:
        text = _read_text(src_path)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConvertError(
                f"Couldn't read this as JSON or CSV ({e}). Supported inputs: "
                ".json, .json.gz, .csv."
            ) from e
        rows = json_to_rows(data)

    if target_format == "csv":
        return rows_to_csv(rows), f"{stem}.csv"
    if target_format == "xlsx":
        return rows_to_xlsx(rows), f"{stem}.xlsx"
    if target_format == "json":
        # Input was CSV -> there's no "original" structure, so JSON of the
        # parsed rows. Input was JSON -> hand back the real original
        # structure, just pretty-printed, rather than the flattened rows.
        payload = rows if is_csv_input else data
        return json.dumps(payload, indent=2, default=str).encode("utf-8"), f"{stem}.json"
    raise ConvertError(f"Unknown target format {target_format!r}")
