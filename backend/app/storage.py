"""
On-disk layout helpers.

    {DATA_ROOT}/raw/{company}/{filename}          <- uploaded MRF
    {DATA_ROOT}/output/{job_id}/part_0001.csv ... <- split CSVs
    {DATA_ROOT}/output/{job_id}/{job_id}.zip      <- packaged archive
    {DATA_ROOT}/_uploads/{upload_id}/chunk_000001 <- in-progress chunk parts
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Strip path components and anything sketchy; keep it recognisable."""
    name = Path(name).name
    name = _SAFE.sub("_", name).strip("._") or "upload"
    return name[:300]


def raw_path(company: str, filename: str) -> Path:
    d = settings.raw_dir / safe_filename(company)
    d.mkdir(parents=True, exist_ok=True)
    return d / safe_filename(filename)


def output_dir_for(job_id: str) -> Path:
    d = settings.output_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def zip_path_for(job_id: str) -> Path:
    return output_dir_for(job_id) / f"{job_id}.zip"


def upload_dir_for(upload_id: str) -> Path:
    d = settings.uploads_tmp_dir / safe_filename(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def chunk_path(upload_id: str, index: int) -> Path:
    return upload_dir_for(upload_id) / f"chunk_{index:06d}"


def discard_upload(upload_id: str) -> None:
    shutil.rmtree(settings.uploads_tmp_dir / safe_filename(upload_id), ignore_errors=True)


def assemble_chunks(upload_id: str, total_chunks: int, dest: Path) -> int:
    """Concatenate chunk_000000..chunk_{n-1} into `dest`. Returns byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as out:
        for i in range(total_chunks):
            part = chunk_path(upload_id, i)
            if not part.exists():
                raise FileNotFoundError(f"missing chunk {i} for upload {upload_id}")
            with open(part, "rb") as pf:
                shutil.copyfileobj(pf, out, length=1024 * 1024)
            size += part.stat().st_size
    discard_upload(upload_id)
    return size


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total
