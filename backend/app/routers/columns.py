"""
GET /api/payers/{company}/columns - what fields does this payer's current
file actually contain? A fast, synchronous probe (not a queued job): it
samples a handful of in-network entries from one real file and reports the
field names, split into the baseline TiC columns vs whatever this payer
adds beyond that. See app/column_probe.py for the mechanics.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import payers
from ..column_probe import ColumnProbeFailed, probe_all_payers, probe_columns

router = APIRouter(prefix="/api", tags=["columns"])


@router.get("/payers/{company}/columns")
def get_payer_columns(
    company: str,
    sample_items: int = Query(150, ge=1, le=2000),
) -> dict:
    try:
        payers.get_source(company)
    except KeyError:
        raise HTTPException(404, f"Unknown payer {company!r}")

    try:
        return probe_columns(company, sample_items=sample_items)
    except ColumnProbeFailed as e:
        raise HTTPException(409, str(e))


@router.get("/payers/columns/download")
def download_all_payer_columns(sample_items: int = Query(100, ge=1, le=2000)):
    """One CSV, one row per (payer, field) pair, across every payer - the
    cross-payer view the single-payer JSON endpoint above can't give you."""
    results = probe_all_payers(sample_items=sample_items)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "payer", "level", "column", "classification",
        "sampled_file", "items_sampled", "error",
    ])
    for r in results:
        rows = [
            ("item", c, "standard") for c in r["item_level_standard_columns"]
        ] + [
            ("item", c, "extra") for c in r["item_level_extra_columns"]
        ] + [
            ("price", c, "standard") for c in r["price_level_standard_columns"]
        ] + [
            ("price", c, "extra") for c in r["price_level_extra_columns"]
        ]
        if not rows:
            # Still one row per failed/empty payer, so it isn't just absent
            # from the report with no explanation.
            writer.writerow([
                r["company"], "", "", "",
                r["sampled_filename"] or "", r["items_sampled"], r["error"] or "",
            ])
            continue
        for level, column, classification in rows:
            writer.writerow([
                r["company"], level, column, classification,
                r["sampled_filename"] or "", r["items_sampled"], r["error"] or "",
            ])

    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="payer_columns_{stamp}.csv"'
        },
    )
