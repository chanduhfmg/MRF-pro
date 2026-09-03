"""
Shared output packaging: take an iterable of dict rows, write them into
CSV parts that roll over at CSV_ROW_THRESHOLD rows, then zip every part
into {job_id}.zip. Used by both the stub and the real pipeline.
"""
from __future__ import annotations

import csv
import logging
import sys
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..config import settings

log = logging.getLogger("mrf.worker.package")

# some MRF rows have huge npis fields; mirror enrich_npi_all.py's bump
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


class CsvSplitWriter:
    """Writes dict rows to part_0001.csv, part_0002.csv, ... rolling over
    every `row_threshold` rows. Each part carries the full header. Flushes
    after every row so an interrupted run keeps everything already written
    (same guarantee the original scripts give)."""

    def __init__(self, out_dir: Path, fieldnames: list[str], row_threshold: int):
        self.out_dir = out_dir
        self.fieldnames = fieldnames
        self.row_threshold = max(1, row_threshold)
        self.part_index = 0
        self.rows_in_part = 0
        self.total_rows = 0
        self._fh = None
        self._writer = None
        self.parts: list[Path] = []

    def _open_next(self) -> None:
        self._close()
        self.part_index += 1
        path = self.out_dir / f"part_{self.part_index:04d}.csv"
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames, extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()
        self.parts.append(path)
        self.rows_in_part = 0

    def write(self, row: dict) -> None:
        if self._writer is None or self.rows_in_part >= self.row_threshold:
            self._open_next()
        self._writer.writerow({k: row.get(k, "") for k in self.fieldnames})
        self._fh.flush()
        self.rows_in_part += 1
        self.total_rows += 1

    def _close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def close(self) -> None:
        self._close()

    def __enter__(self) -> "CsvSplitWriter":
        return self

    def __exit__(self, *exc) -> None:
        self._close()


def write_split_csvs(
    rows: Iterable[dict],
    out_dir: Path,
    fieldnames: list[str],
    *,
    row_threshold: int | None = None,
    on_row: "callable | None" = None,
) -> tuple[list[Path], int]:
    """Returns (csv_part_paths, total_rows)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    threshold = row_threshold or settings.csv_row_threshold
    with CsvSplitWriter(out_dir, fieldnames, threshold) as w:
        for row in rows:
            w.write(row)
            if on_row is not None:
                on_row(w.total_rows)
    return w.parts, w.total_rows


def zip_parts(job_id: str, out_dir: Path, part_paths: list[Path]) -> tuple[Path, int]:
    """Zip the CSV parts (plus any manifest.json present) into {job_id}.zip.
    Returns (zip_path, zip_size_bytes)."""
    zip_path = out_dir / f"{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in part_paths:
            if p.exists():
                zf.write(p, arcname=p.name)
        manifest = out_dir / "manifest.json"
        if manifest.exists():
            zf.write(manifest, arcname="manifest.json")
    return zip_path, zip_path.stat().st_size


def iter_csv_rows(path: Path) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)
