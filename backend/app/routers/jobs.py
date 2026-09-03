"""
/api/jobs router.

Chunked upload flow (large MRFs never land fully in memory):

    POST /api/jobs/uploads              -> { upload_id, chunk_size }
    POST /api/jobs/uploads/{id}/chunks  -> (multipart: index, chunk) append part
    POST /api/jobs/uploads/{id}/complete-> assemble -> create job (queued)

Plus a convenience single-shot POST /api/jobs for small files / quick tests.
The polling worker picks up queued jobs on its own; nothing runs inline here.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import crud, storage
from ..config import settings
from ..constants import ACCEPTED_EXTENSIONS, INSURANCE_COMPANY_KEYS
from ..database import get_db
from ..models import JobStatus
from ..utils import utcnow
from ..schemas import (
    JobListOut,
    JobOut,
    JobStatusOut,
    UploadFinishIn,
    UploadInitIn,
    UploadInitOut,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _validate_company(key: str) -> str:
    key = (key or "").strip().lower()
    if key not in INSURANCE_COMPANY_KEYS:
        raise HTTPException(422, f"Unknown insurance_company '{key}'")
    return key


def _validate_ext(filename: str) -> None:
    low = filename.lower()
    if not low.endswith(ACCEPTED_EXTENSIONS):
        raise HTTPException(
            422, f"File must be one of {ACCEPTED_EXTENSIONS} (got '{filename}')"
        )


# --------------------------------------------------------------------------
# Chunked upload
# --------------------------------------------------------------------------

@router.post("/uploads", response_model=UploadInitOut)
def init_upload(body: UploadInitIn) -> UploadInitOut:
    _validate_company(body.insurance_company)
    _validate_ext(body.filename)
    if settings.max_upload_bytes and body.file_size > settings.max_upload_bytes:
        raise HTTPException(
            413,
            f"File is {body.file_size} bytes; limit is {settings.max_upload_bytes}.",
        )

    import uuid as _uuid

    upload_id = str(_uuid.uuid4())
    d = storage.upload_dir_for(upload_id)
    meta = {
        "insurance_company": _validate_company(body.insurance_company),
        "filename": body.filename,
        "file_size": body.file_size,
        "created_at": utcnow().isoformat(),
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return UploadInitOut(upload_id=upload_id, chunk_size=settings.upload_chunk_size)


@router.post("/uploads/{upload_id}/chunks")
async def upload_chunk(
    upload_id: str,
    index: int = Form(...),
    chunk: UploadFile = File(...),
) -> dict:
    d = settings.uploads_tmp_dir / storage.safe_filename(upload_id)
    if not (d / "meta.json").exists():
        raise HTTPException(404, "Unknown or expired upload_id")
    if index < 0:
        raise HTTPException(422, "index must be >= 0")

    dest = storage.chunk_path(upload_id, index)
    written = 0
    with open(dest, "wb") as f:
        while True:
            buf = await chunk.read(1024 * 1024)
            if not buf:
                break
            f.write(buf)
            written += len(buf)
    return {"index": index, "bytes": written}


@router.post("/uploads/{upload_id}/complete", response_model=JobOut)
def complete_upload(
    upload_id: str,
    body: UploadFinishIn,
    db: Session = Depends(get_db),
) -> JobOut:
    d = settings.uploads_tmp_dir / storage.safe_filename(upload_id)
    meta_file = d / "meta.json"
    if not meta_file.exists():
        raise HTTPException(404, "Unknown or expired upload_id")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))

    company = meta["insurance_company"]
    filename = storage.safe_filename(meta["filename"])
    dest = storage.raw_path(company, filename)

    # don't clobber an existing raw file with the same name -> suffix it
    if dest.exists():
        stem = dest.name
        i = 1
        while dest.exists():
            dest = dest.with_name(f"{i}_{stem}")
            i += 1

    try:
        size = storage.assemble_chunks(upload_id, body.total_chunks, dest)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e

    job = crud.create_job(
        db,
        insurance_company=company,
        source_filename=dest.name,
        source_file_path=str(dest),
        total_bytes=size,
    )
    return JobOut.from_job(job)


@router.delete("/uploads/{upload_id}")
def abort_upload(upload_id: str) -> dict:
    storage.discard_upload(upload_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# Single-shot upload (small files / quick test) - still not processed inline
# --------------------------------------------------------------------------

@router.post("", response_model=JobOut)
async def create_job_single(
    insurance_company: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> JobOut:
    company = _validate_company(insurance_company)
    _validate_ext(file.filename or "upload.json")

    dest = storage.raw_path(company, file.filename or "upload.json")
    if dest.exists():
        dest = dest.with_name(f"{int(utcnow().timestamp())}_{dest.name}")

    size = 0
    with open(dest, "wb") as f:
        while True:
            buf = await file.read(4 * 1024 * 1024)
            if not buf:
                break
            f.write(buf)
            size += len(buf)
            if settings.max_upload_bytes and size > settings.max_upload_bytes:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Upload exceeds MAX_UPLOAD_BYTES")

    job = crud.create_job(
        db,
        insurance_company=company,
        source_filename=dest.name,
        source_file_path=str(dest),
        total_bytes=size,
    )
    return JobOut.from_job(job)


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------

@router.get("", response_model=JobListOut)
def list_jobs(
    company: str | None = Query(None),
    status: JobStatus | None = Query(None),
    date_from: str | None = Query(None, description="YYYY-MM-DD"),
    date_to: str | None = Query(None, description="YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> JobListOut:
    df = _parse_date(date_from, end=False)
    dt = _parse_date(date_to, end=True)
    rows, total = crud.list_jobs(
        db,
        company=company,
        status=status.value if status else None,
        date_from=df,
        date_to=dt,
        limit=limit,
        offset=offset,
    )
    return JobListOut(
        jobs=[JobOut.from_job(j) for j in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobOut.from_job(job)


@router.get("/{job_id}/status", response_model=JobStatusOut)
def get_job_status(job_id: str, db: Session = Depends(get_db)) -> JobStatusOut:
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobStatusOut.from_job(job)


@router.get("/{job_id}/download")
def download_output(job_id: str, db: Session = Depends(get_db)):
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.completed or not job.output_zip_path:
        raise HTTPException(409, "Job output is not ready")
    zip_path = Path(job.output_zip_path)
    if not zip_path.exists():
        raise HTTPException(410, "Output archive is missing from disk")

    fname = f"{job.insurance_company}_{Path(job.source_filename).stem}_{job.id[:8]}.zip"
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=fname,
    )


def _parse_date(value: str | None, *, end: bool) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as e:
        raise HTTPException(422, f"Bad date '{value}', expected YYYY-MM-DD") from e
    return datetime.combine(d, dtime.max if end else dtime.min)
