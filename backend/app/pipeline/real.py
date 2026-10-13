"""
Real extraction pipeline: thin orchestration over the adapted CLI scripts
(`extract_tic_all.py` + `enrich_npi_all.py` in the repo root). The heavy
lifting - ijson streaming, provider-group lookup, the NPI==TIN corruption
guard, NPPES enrichment - lives in those scripts and is reused as-is via
their generator entrypoints; nothing is reimplemented here.

Enable with EXTRACTION_MODE=real (the default).

Job sources
-----------
  * **Uploaded** - `source_file_path` already points at a file on disk.
  * **Payer-sourced** - `source_url` is set (the job came out of an NPI/TIN
    search), so the worker downloads it first, as a real phase with its own
    progress and ETA.

Enrichment is never a separate user action: extraction and NPPES enrichment
are one streaming generator chain, so rows are enriched as they are produced.

Progress + ETA model
--------------------
Phases are given estimated DURATIONS from the file size and the throughput
learned on earlier jobs (see eta.PhasePlan); the running phase is projected
from its live rate. Percent is time-proportional, so a 20-second download
never looks like a fifth of a two-hour job, and the ETA is a countdown that
does not jump between phases.

Extraction and enrichment are interleaved (the enrich generator pulls from
the extract generator in 400-row batches), so they are modelled as ONE phase
driven by bytes read through the in_network pass. Enrichment cost shows up
naturally as a slower bytes/second in that phase - and that rate is learned
separately for narrowed vs whole-network jobs, because they differ 10x.

Outputs
-------
  part_NNNN.csv   every extracted row; ';'-joined per-NPI columns
  providers.csv   one line per NPI (name, address, ZIP, TINs, row count)
  manifest.json   what was asked for and what happened
  {job}.zip       all of the above
  output.xlsx     sheets rates / providers / summary (when it fits a sheet)
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
    STAGE_DOWNLOADING,
    STAGE_ENRICHING,
    STAGE_EXTRACTING,
    STAGE_PACKAGING,
)
from ..eta import PhasePlan, learn_phase_rate, load_phase_rates
from .context import ProgressReporter
from .result import PipelineResult

log = logging.getLogger("mrf.worker.real")


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


def _est(rates: dict, key: str, nbytes: int, floor: float = 2.0) -> float:
    rate = rates.get(key) or 1.0
    return max(floor, (nbytes or 0) / rate)


def run_real(job, reporter: ProgressReporter, out_dir: Path) -> PipelineResult:
    extract_mod, enrich_mod = _load_scripts()

    src = Path(job.source_file_path)
    needs_download = bool(getattr(job, "source_url", None)) and not src.exists()
    total_bytes = job.total_bytes or (src.stat().st_size if src.exists() else 0)

    target_kind = job.target_type if job.filter_to_target else None
    target_value = job.target_value if job.filter_to_target else None
    scope = "narrowed" if target_kind else "all"
    extract_key = f"extract:{scope}"

    # ---- the plan: estimated seconds per phase from learned throughput -----
    rates = load_phase_rates()
    phases: list[tuple[str, float]] = []
    if needs_download:
        phases.append(("download", _est(rates, "download", total_bytes)))
    phases += [
        ("lookup", _est(rates, "lookup", total_bytes)),
        ("extract", _est(rates, extract_key, total_bytes)),
        ("package", _est(rates, "package", total_bytes, floor=4.0)),
    ]
    plan = PhasePlan(phases)
    log.info(
        "job %s: plan %s (scope=%s, %.0f MB)",
        job.id, {n: round(s) for n, s in phases}, scope, total_bytes / 1e6,
    )

    def _push(stage: str | None = None, *, records: int | None = None,
              downloaded: int | None = None, bps: float | None = None) -> None:
        reporter.update(
            stage=stage,
            bytes_processed=int(plan.fraction() * (total_bytes or 1)),
            total_bytes=total_bytes or None,
            records_processed=records,
            downloaded_bytes=downloaded,
            eta_seconds=plan.eta_seconds(),
            throughput_bps=bps,
        )

    # ---- Phase 0: download (payer-sourced jobs only) ---------------------
    if needs_download:
        from ..net import download_to

        plan.start("download")
        reporter.set_stage(STAGE_DOWNLOADING)
        url = job.source_url
        log.info("job %s: downloading %s", job.id, url)

        def _dl_progress(done: int, total: int | None, rate: float | None) -> None:
            nonlocal total_bytes
            if total and total > 0:
                if not total_bytes or abs(total - total_bytes) > 0.1 * total_bytes:
                    # first real size, or the search's size was wrong: re-plan
                    total_bytes = total
                    plan.set_estimate("lookup", _est(rates, "lookup", total))
                    plan.set_estimate("extract", _est(rates, extract_key, total))
                    plan.set_estimate("package", _est(rates, "package", total, floor=4.0))
                plan.observe(done / total)
            else:
                plan.observe(min(0.95, done / (total_bytes or (done + 1))))
            _push(STAGE_DOWNLOADING, downloaded=done, bps=rate)

        src.parent.mkdir(parents=True, exist_ok=True)
        size = download_to(url, src, timeout=settings.download_timeout, progress=_dl_progress)
        total_bytes = size or total_bytes
        plan.finish("download")
        learn_phase_rate("download", size, plan.actual_seconds("download") or 0)
        log.info("job %s: downloaded %d bytes to %s", job.id, size, src)

    if not src.exists():
        raise FileNotFoundError(
            f"source MRF file missing: {src}"
            + (" (and no source_url to fetch it from)" if not job.source_url else "")
        )

    total_bytes = total_bytes or src.stat().st_size or 1
    reporter.total_bytes = total_bytes

    fieldnames = list(extract_mod.CSV_FIELDNAMES) + list(enrich_mod.ENRICH_COLUMNS)

    if target_kind:
        log.info("job %s: filtering output to %s=%s", job.id, target_kind, target_value)

    target_codes: list[str] | None = None
    if getattr(job, "target_codes", None):
        try:
            parsed = json.loads(job.target_codes)
            if isinstance(parsed, list) and parsed:
                target_codes = [str(c) for c in parsed]
        except Exception:
            log.warning("job %s: unreadable target_codes, using default set", job.id)
    if target_codes:
        log.info("job %s: extracting %d custom code(s)", job.id, len(target_codes))
    # A targeted job (TIN/NPI filter) with no explicit code list captures
    # every code that provider bills, not the generic default set - so
    # there's no fixed "requested" count to report ahead of time; it's
    # however many distinct codes turn up (see matched_code_count below).
    all_codes_for_target = bool(target_kind) and not target_codes
    requested_code_count = (
        None if all_codes_for_target
        else len(target_codes) if target_codes
        else len(extract_mod.TARGET_CODES)
    )

    # ---- Phase 1: provider lookup ---------------------------------------
    plan.start("lookup")
    reporter.set_stage(STAGE_BUILDING_LOOKUP)

    def _lookup_progress(**kw):
        tb = kw.get("total_bytes") or total_bytes
        plan.observe((kw.get("bytes_read", 0) / tb) if tb else 0.0)
        _push(STAGE_BUILDING_LOOKUP)

    provider_lookup = extract_mod.build_provider_lookup(str(src), progress=_lookup_progress)
    plan.finish("lookup")
    learn_phase_rate("lookup", total_bytes, plan.actual_seconds("lookup") or 0)
    log.info("job %s: loaded %d provider groups", job.id, len(provider_lookup))

    # ---- Phase 2: extract + enrich (one fused streaming phase) ------------
    extract_stats: dict = {}
    enrich_stats: dict = {}
    directory: dict = {}       # npi -> NPPES info, filled by the enricher
    npi_tins: dict = {}
    npi_rows: dict = {}

    plan.start("extract")
    _push(STAGE_EXTRACTING)

    def _extract_progress(**kw):
        plan.observe((kw.get("bytes_read", 0) / total_bytes) if total_bytes else 0.0)
        _push(STAGE_EXTRACTING, records=kw.get("rows", 0))

    def _enrich_progress(**kw):
        # same phase; enrichment just moves the record counter and the label
        _push(STAGE_ENRICHING, records=kw.get("rows", 0))

    rate_rows = extract_mod.iter_rate_rows(
        str(src),
        provider_lookup=provider_lookup,
        progress=_extract_progress,
        stats=extract_stats,
        target_kind=target_kind,
        target_value=target_value,
        target_codes=target_codes,
    )
    enriched_rows = enrich_mod.iter_enriched_rows(
        rate_rows,
        threads=settings.npi_lookup_threads,
        max_npis_per_row=settings.npi_max_per_row,
        progress=_enrich_progress,
        stats=enrich_stats,
        cache=directory,
    )

    from .package import (
        track_npis,
        write_providers_csv,
        write_split_csvs,
        write_xlsx,
        zip_parts,
    )

    def _on_row(n: int) -> None:
        if n % 2000 == 0:
            _push(records=n)

    from ..naming import csv_part_prefix

    parts, total_rows = write_split_csvs(
        track_npis(enriched_rows, npi_tins, npi_rows), out_dir, fieldnames,
        row_threshold=settings.csv_row_threshold,
        max_parts=settings.csv_max_parts,
        prefix=csv_part_prefix(job, target_codes),
        on_row=_on_row,
    )
    plan.finish("extract")
    learn_phase_rate(extract_key, total_bytes, plan.actual_seconds("extract") or 0)

    # ---- Phase 3: package ------------------------------------------------
    from .package import XLSX_MAX_ROWS

    plan.start("package")
    # rows in, rows out: csv->zip is fast, the workbook is the slow part
    plan.set_estimate(
        "package",
        3.0 + total_rows / 25_000 + (total_rows / 6_000 if total_rows <= XLSX_MAX_ROWS else 0),
    )
    _push(STAGE_PACKAGING, records=total_rows)

    providers_csv = write_providers_csv(out_dir, directory, npi_tins, npi_rows)
    plan.observe(0.15)
    _push(STAGE_PACKAGING)

    manifest = {
        "job_id": job.id,
        "insurance_company": job.insurance_company,
        "plan_name": job.plan_name,
        "source_filename": job.source_filename,
        "source_url": job.source_url,
        "source_size_bytes": total_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": total_rows,
        "provider_count": len(directory),
        "csv_parts": [p.name for p in parts],
        "providers_csv": providers_csv.name if providers_csv else None,
        "csv_row_threshold": settings.csv_row_threshold,
        "csv_max_parts": settings.csv_max_parts,
        "npi_max_per_row": settings.npi_max_per_row,
        "search_id": job.search_id,
        "target": ({"type": target_kind, "value": target_value} if target_kind else None),
        "target_codes": target_codes,
        "code_set_name": job.code_set_name,
        # Phases finished so far. `package` is absent by design: the manifest
        # has to be written before it goes into the zip, which is part of
        # packaging. `is not None` matters - a phase that took 0.0s is a
        # completed phase, not a missing one.
        "phase_seconds": {
            n: round(plan.actual_seconds(n), 2)
            for n in plan.order
            if plan.actual_seconds(n) is not None
        },
        "extract_stats": extract_stats,
        "enrich_stats": enrich_stats,
        "columns": fieldnames,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    zip_path, zip_size = zip_parts(job.id, out_dir, parts)
    plan.observe(0.4)
    _push(f"{STAGE_PACKAGING} — writing Excel", records=total_rows)

    xlsx_rows_done = {"n": 0}

    def _xlsx_progress(n: int) -> None:
        xlsx_rows_done["n"] = n
        plan.observe(0.4 + 0.6 * min(1.0, n / max(1, total_rows)))
        _push(records=total_rows)

    xlsx_path = write_xlsx(
        parts, out_dir, fieldnames, total_rows,
        summary={
            "payer": job.insurance_company,
            "plan": job.plan_name,
            "source_file": job.source_filename,
            "target": f"{target_kind} {target_value}" if target_kind else "all provider groups",
            "codes": (
                ", ".join(target_codes) if target_codes
                else "all codes billed by this provider" if all_codes_for_target
                else "default set (E&M + ophthalmic)"
            ),
            "code_set": job.code_set_name,
            "rows": total_rows,
            "providers": len(directory),
            "items_scanned": extract_stats.get("items_scanned"),
            "dropped_off_target": extract_stats.get("dropped_off_target"),
            "dropped_npi_eq_tin": extract_stats.get("dropped_npi_eq_tin"),
            "codes_matched": ", ".join(extract_stats.get("matched_codes", [])),
            "generated_at": manifest["generated_at"],
        },
        providers_csv=providers_csv,
        on_row=_xlsx_progress,
    )
    if xlsx_path is None:
        log.info("job %s: %d rows is above the Excel sheet limit; zip only", job.id, total_rows)

    plan.finish("package")
    learn_phase_rate("package", total_bytes, plan.actual_seconds("package") or 0)
    # flush, don't just update: progress writes are throttled, and a short job
    # would otherwise leave its last published percent short of 100 until
    # mark_completed catches up.
    reporter.update(bytes_processed=total_bytes, records_processed=total_rows, eta_seconds=0.0)
    reporter.flush()

    # A downloaded MRF is a multi-GB scratch artifact; the CSVs are the
    # deliverable. Keep it only when explicitly asked to.
    if job.source_url and not settings.keep_downloaded_mrf:
        try:
            src.unlink(missing_ok=True)
            log.info("job %s: removed downloaded source %s", job.id, src)
        except Exception:
            log.warning("job %s: could not remove %s", job.id, src)

    matched_codes = extract_stats.get("matched_codes") or []
    log.info(
        "job %s complete: %d rows, %d providers, %d part(s), %d bytes zipped, "
        "%d/%d requested code(s) matched %s "
        "(dropped npi==tin: extract=%s enrich=%s; off-target: %s) phases=%s",
        job.id, total_rows, len(directory), len(parts), zip_size,
        len(matched_codes), requested_code_count, matched_codes,
        extract_stats.get("dropped_npi_eq_tin"),
        enrich_stats.get("rows_skipped_npi_eq_tin"),
        extract_stats.get("dropped_off_target"),
        manifest["phase_seconds"],
    )
    return PipelineResult(
        record_count=total_rows,
        output_file_count=len(parts),
        zip_path=zip_path,
        zip_size_bytes=zip_size,
        xlsx_path=xlsx_path,
        requested_code_count=requested_code_count,
        matched_code_count=len(matched_codes),
        matched_codes=matched_codes,
    )
