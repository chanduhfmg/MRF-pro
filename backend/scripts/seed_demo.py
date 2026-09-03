"""
Seed a spread of fake jobs so the Jobs list + KPI dashboard have something
to show before you run a real extraction.

    cd backend
    python -m scripts.seed_demo           # add ~24 demo jobs
    python -m scripts.seed_demo --wipe    # delete all jobs first
"""
from __future__ import annotations

import random
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.constants import INSURANCE_COMPANIES  # noqa: E402
from app.utils import utcnow  # noqa: E402
from app.database import session_scope, init_db  # noqa: E402
from app.models import ExtractionJob, JobStatus  # noqa: E402

COMPANIES = [c["key"] for c in INSURANCE_COMPANIES]


def make_job(days_ago: int) -> ExtractionJob:
    created = utcnow() - timedelta(days=days_ago, hours=random.randint(0, 20))
    status = random.choices(
        [JobStatus.completed, JobStatus.failed, JobStatus.processing, JobStatus.queued],
        weights=[70, 12, 10, 8],
    )[0]
    company = random.choice(COMPANIES)
    total = random.randint(150, 4200) * 1024 * 1024
    j = ExtractionJob(
        id=str(uuid.uuid4()),
        insurance_company=company,
        source_filename=f"{company}_in-network-rates_{created:%Y-%m}.json.gz",
        source_file_path=f"/data/raw/{company}/demo_{created:%Y%m%d}.json.gz",
        status=status,
        created_at=created,
        total_bytes=total,
    )
    if status in (JobStatus.completed, JobStatus.failed, JobStatus.processing):
        j.started_at = created + timedelta(seconds=random.randint(2, 40))
    if status == JobStatus.processing:
        j.bytes_processed = int(total * random.uniform(0.1, 0.85))
        j.records_processed = random.randint(1000, 90000)
        j.current_stage = random.choice(["Extracting rates", "Enriching NPIs", "Building provider lookup"])
    if status == JobStatus.completed:
        rc = random.randint(5000, 480000)
        j.completed_at = j.started_at + timedelta(minutes=random.randint(3, 210))
        j.bytes_processed = total
        j.records_processed = rc
        j.record_count = rc
        j.output_file_count = max(1, rc // 100000 + 1)
        j.output_size_bytes = int(total * random.uniform(0.02, 0.12))
        j.output_zip_path = f"/data/output/{j.id}/{j.id}.zip"
        j.current_stage = "Completed"
    if status == JobStatus.failed:
        j.completed_at = j.started_at + timedelta(minutes=random.randint(1, 45))
        j.error_message = random.choice([
            "RuntimeError: provider_references array not found in file",
            "ConnectionError: NPPES lookup failed repeatedly",
            "ValueError: unexpected billing_code_type 'RC'",
        ])
        j.current_stage = "Extracting rates"
    return j


def main() -> None:
    init_db()
    wipe = "--wipe" in sys.argv
    with session_scope() as db:
        if wipe:
            n = db.query(ExtractionJob).delete()
            print(f"deleted {n} existing job(s)")
        for d in range(0, 26):
            for _ in range(random.randint(0, 2)):
                db.add(make_job(d))
    print("demo jobs seeded.")


if __name__ == "__main__":
    main()
