"""
Stub extraction: no real parsing. Produces believable progress over ~30s
and writes real (fake-content) CSV rows so packaging, download,
record_count and the KPI numbers are all exercised end-to-end before the
real ijson logic is plugged in (build order step 2 vs step 6).

Enable with EXTRACTION_MODE=stub.
"""
from __future__ import annotations

import logging
import random
import time

from ..constants import (
    STAGE_BUILDING_LOOKUP,
    STAGE_ENRICHING,
    STAGE_EXTRACTING,
    STAGE_PACKAGING,
)
from .context import ProgressReporter
from .result import PipelineResult

log = logging.getLogger("mrf.worker.stub")

_FAKE_FIELDS = [
    "billing_code", "billing_code_type", "name", "negotiated_type",
    "negotiated_rate", "billing_class", "service_code",
    "provider_group_id", "tin", "npis", "npi_count",
    "provider_names", "organization_names", "provider_states",
    "provider_zips", "provider_zip5s", "primary_zip",
]
_CODES = ["99213", "99214", "92014", "92250", "66984", "99204", "92133"]
_ZIPS = ["14604", "10001", "75201", "33130", "19103", "94105", "60601"]


def _sleep_scaled(total_bytes: int | None, base: float) -> float:
    """Bigger 'files' take a little longer, but keep the stub snappy."""
    if not total_bytes:
        return base
    return base + min(6.0, total_bytes / (500 * 1024 * 1024))


def run_stub(job, reporter: ProgressReporter, out_dir) -> PipelineResult:
    from .package import write_split_csvs, zip_parts

    total_bytes = job.total_bytes or random.randint(200, 900) * 1024 * 1024
    reporter.total_bytes = total_bytes

    # --- Stage 1: build provider lookup (0 -> ~25%) ---
    reporter.set_stage(STAGE_BUILDING_LOOKUP)
    steps = 12
    for i in range(1, steps + 1):
        time.sleep(_sleep_scaled(total_bytes, 0.5) / steps * 3)
        reporter.update(bytes_processed=int(total_bytes * 0.25 * i / steps))

    # --- Stage 2: extract rates (25% -> 70%) ---
    reporter.set_stage(STAGE_EXTRACTING)
    n_rows = random.randint(1500, 6000)
    rows_buffer: list[dict] = []
    for r in range(n_rows):
        rows_buffer.append(_fake_row(r))
        if r % 200 == 0:
            frac = r / n_rows
            reporter.update(
                bytes_processed=int(total_bytes * (0.25 + 0.45 * frac)),
                records_processed=r,
            )
            time.sleep(_sleep_scaled(total_bytes, 0.4) / (n_rows / 200))

    # --- Stage 3: enrich NPIs (70% -> 92%) ---
    reporter.set_stage(STAGE_ENRICHING)
    for i in range(1, 11):
        time.sleep(_sleep_scaled(total_bytes, 0.35) / 10 * 2)
        reporter.update(
            bytes_processed=int(total_bytes * (0.70 + 0.22 * i / 10)),
            records_processed=n_rows,
        )

    # --- Stage 4: package (92% -> 100%) ---
    reporter.set_stage(STAGE_PACKAGING)
    parts, total_rows = write_split_csvs(iter(rows_buffer), out_dir, _FAKE_FIELDS)
    zip_path, zip_size = zip_parts(job.id, out_dir, parts)
    reporter.update(bytes_processed=total_bytes, records_processed=total_rows)

    log.info("stub job %s produced %d rows in %d part(s)", job.id, total_rows, len(parts))
    return PipelineResult(
        record_count=total_rows,
        output_file_count=len(parts),
        zip_path=zip_path,
        zip_size_bytes=zip_size,
    )


def _fake_row(i: int) -> dict:
    code = random.choice(_CODES)
    npis = [str(random.randint(1_000_000_000, 1_999_999_999)) for _ in range(random.randint(1, 4))]
    zips = [random.choice(_ZIPS) for _ in npis]
    return {
        "billing_code": code,
        "billing_code_type": "CPT",
        "name": f"Sample service {code}",
        "negotiated_type": random.choice(["negotiated", "fee schedule", "percentage"]),
        "negotiated_rate": round(random.uniform(40, 480), 2),
        "billing_class": random.choice(["professional", "institutional"]),
        "service_code": random.choice(["11", "22", "19", "21"]),
        "provider_group_id": random.randint(1, 400),
        "tin": str(random.randint(100_000_000, 999_999_999)),
        "npis": ";".join(npis),
        "npi_count": len(npis),
        "provider_names": ";".join(f"Provider {n[-4:]}" for n in npis),
        "organization_names": random.choice(["", "Sample Health System", "Vision Partners LLC"]),
        "provider_states": ";".join(random.choice(["NY", "CA", "TX", "FL", "PA"]) for _ in npis),
        "provider_zips": ";".join(f"{z}{random.randint(1000, 9999)}" for z in zips),
        "provider_zip5s": ";".join(zips),
        "primary_zip": zips[0] if zips else "",
    }
