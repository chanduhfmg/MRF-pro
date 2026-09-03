"""
Real extraction pipeline: thin orchestration over the adapted CLI scripts
(`extract_tic_all.py` + `enrich_npi_all.py` in the repo root). The heavy
lifting - ijson streaming, provider-group lookup, the NPI==TIN corruption
guard, NPPES enrichment - lives in those scripts and is reused as-is via
their generator entrypoints; nothing is reimplemented here.

Enable with EXTRACTION_MODE=real (the default).

Progress model
--------------
Three heavy phases feed one progress bar. `bytes_processed` is advanced
through fixed bands of `total_bytes` (the real source file size), so the
spec's `percent = bytes_processed/total_bytes` and
`eta = elapsed/bytes_processed*remaining` stay monotonic and meaningful:

    Building provider lookup   0%  -> 30%   (band from bytes read, pass 1)
    Extracting rates          30%  -> 75%   (band from bytes read, pass 2)
    Enriching NPIs            75%  -> 92%   (band from rows enriched)
    Packaging                 92%  -> 100%
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..config import REPO_ROOT, settings
from ..constants import (
    STAGE_BUILDING_LOOKUP,
    STAGE_ENRICHING,
    STAGE_EXTRACTING,
    STAGE_PACKAGING,
)
from .context import ProgressReporter
from .result import PipelineResult

log = logging.getLogger("mrf.worker.real")

_BAND_LOOKUP = (0.00, 0.30)
_BAND_EXTRACT = (0.30, 0.75)
_BAND_ENRICH = (0.75, 0.92)
_BAND_PACKAGE = (0.92, 1.00)


def _load_scripts():
    """Import the repo-root scripts without running their __main__ blocks."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        import extract_tic_all as extract_mod  # type: ignore
        import enrich_npi_all as enrich_mod    # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RuntimeError(
            f"Could not import extraction scripts from {REPO_ROOT}: {e}. "
            "Expected extract_tic_all.py and enrich_npi_all.py in the repo root."
        ) from e
    return extract_mod, enrich_mod


def _band(frac: float, band: tuple[float, float]) -> float:
    lo, hi = band
    return lo + max(0.0, min(1.0, frac)) * (hi - lo)


def run_real(job, reporter: ProgressReporter, out_dir: Path) -> PipelineResult:
    extract_mod, enrich_mod = _load_scripts()

    src = Path(job.source_file_path)
    if not src.exists():
        raise FileNotFoundError(f"source MRF file missing: {src}")

    total_bytes = job.total_bytes or src.stat().st_size or 1
    reporter.total_bytes = total_bytes

    fieldnames = list(extract_mod.CSV_FIELDNAMES) + list(enrich_mod.ENRICH_COLUMNS)

    # ---- Phase 1: provider lookup ---------------------------------------
    reporter.set_stage(STAGE_BUILDING_LOOKUP)

    def _lookup_progress(**kw):
        frac = (kw.get("bytes_read", 0) / kw.get("total_bytes", total_bytes)) if kw.get("total_bytes") else 0.0
        reporter.update(bytes_processed=int(_band(frac, _BAND_LOOKUP) * total_bytes))

    provider_lookup = extract_mod.build_provider_lookup(str(src), progress=_lookup_progress)
    log.info("job %s: loaded %d provider groups", job.id, len(provider_lookup))
    reporter.update(
        stage=STAGE_EXTRACTING,
        bytes_processed=int(_BAND_EXTRACT[0] * total_bytes),
    )

    # ---- Phase 2+3: extract -> enrich -> split CSVs (fused, streaming) --
    extract_stats: dict = {}
    enrich_stats: dict = {}

    def _extract_progress(**kw):
        frac = (kw.get("bytes_read", 0) / total_bytes) if total_bytes else 0.0
        reporter.update(
            stage=STAGE_EXTRACTING,
            bytes_processed=int(_band(frac, _BAND_EXTRACT) * total_bytes),
            records_processed=kw.get("rows", 0),
        )

    # est. rows for the enrich band: unknown until extraction finishes, so
    # fall back to a rolling estimate keyed off rows seen so far.
    def _enrich_progress(**kw):
        rows = kw.get("rows", 0)
        est_total = max(extract_stats.get("rows", 0), rows, 1)
        frac = rows / est_total
        reporter.update(
            stage=STAGE_ENRICHING,
            bytes_processed=int(_band(frac, _BAND_ENRICH) * total_bytes),
            records_processed=rows,
        )

    rate_rows = extract_mod.iter_rate_rows(
        str(src),
        provider_lookup=provider_lookup,
        progress=_extract_progress,
        stats=extract_stats,
    )
    enriched_rows = enrich_mod.iter_enriched_rows(
        rate_rows,
        threads=settings.npi_lookup_threads,
        max_npis_per_row=settings.npi_max_per_row,
        progress=_enrich_progress,
        stats=enrich_stats,
    )

    from .package import write_split_csvs, zip_parts

    def _on_row(n: int) -> None:
        if n % 2000 == 0:
            reporter.update(records_processed=n)

    parts, total_rows = write_split_csvs(
        enriched_rows, out_dir, fieldnames,
        row_threshold=settings.csv_row_threshold, on_row=_on_row,
    )

    # ---- Phase 4: package --------------------------------------------------
    reporter.update(
        stage=STAGE_PACKAGING,
        bytes_processed=int(_BAND_PACKAGE[0] * total_bytes),
        records_processed=total_rows,
    )

    manifest = {
        "job_id": job.id,
        "insurance_company": job.insurance_company,
        "source_filename": job.source_filename,
        "source_size_bytes": total_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": total_rows,
        "csv_parts": [p.name for p in parts],
        "csv_row_threshold": settings.csv_row_threshold,
        "npi_max_per_row": settings.npi_max_per_row,
        "extract_stats": extract_stats,
        "enrich_stats": enrich_stats,
        "columns": fieldnames,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    zip_path, zip_size = zip_parts(job.id, out_dir, parts)
    reporter.update(bytes_processed=total_bytes, records_processed=total_rows)

    log.info(
        "job %s complete: %d rows, %d part(s), %d bytes zipped (dropped npi==tin: extract=%s enrich=%s)",
        job.id, total_rows, len(parts), zip_size,
        extract_stats.get("dropped_npi_eq_tin"), enrich_stats.get("rows_skipped_npi_eq_tin"),
    )
    return PipelineResult(
        record_count=total_rows,
        output_file_count=len(parts),
        zip_path=zip_path,
        zip_size_bytes=zip_size,
    )
