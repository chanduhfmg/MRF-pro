"""
POST /api/convert - the "Convert File" sidebar tool. Upload any JSON/
JSON.GZ/CSV file, pick an output format, get a download back. Single-shot:
streamed to a temp file first (so a browser hiccup mid-upload doesn't leave
a half-read buffer around), then converted, then the temp file is removed
either way. Capped well below the multi-GB MRF path - see
file_convert.MAX_CONVERT_BYTES.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from ..file_convert import MAX_CONVERT_BYTES, ConvertError, convert_file

router = APIRouter(prefix="/api", tags=["convert"])

_MEDIA = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
}


@router.post("/convert")
async def convert_uploaded_file(
    file: UploadFile = File(...),
    target_format: str = Form(...),
) -> Response:
    target_format = target_format.strip().lower()
    if target_format not in _MEDIA:
        raise HTTPException(422, f"target_format must be one of {sorted(_MEDIA)}")

    filename = file.filename or "upload"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
            size = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CONVERT_BYTES:
                    raise HTTPException(
                        413,
                        f"File is over the {MAX_CONVERT_BYTES // (1024 * 1024)} MB "
                        "limit for this tool - it's meant for one-off files, not "
                        "multi-GB MRFs (use Search + Extract for those).",
                    )
                tmp.write(chunk)

        output_bytes, output_name = convert_file(tmp_path, filename, target_format)
    except ConvertError as e:
        raise HTTPException(422, str(e))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return Response(
        content=output_bytes,
        media_type=_MEDIA[target_format],
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )
