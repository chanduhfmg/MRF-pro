"""
/api/code-sets — named, reusable CPT/HCPCS code lists.

    GET    /api/code-sets           every saved set, alphabetical
    POST   /api/code-sets           {name, codes[]}  -> 409 if the name exists
    PUT    /api/code-sets/{id}      rename and/or replace the codes
    DELETE /api/code-sets/{id}

Names are unique case-insensitively: a dropdown with two "Cardiology"
entries would be worse than no dropdown at all.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud
from ..database import get_db
from ..schemas import CodeSetIn, CodeSetOut, CodeSetUpdateIn

log = logging.getLogger("mrf.api.code_sets")

router = APIRouter(prefix="/api/code-sets", tags=["code-sets"])

# CPT is 5 digits; HCPCS is a letter + 4 digits; some payers publish modifiers
# or custom codes. Be permissive on shape, strict on garbage.
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
MAX_CODES_PER_SET = 2000


def clean_codes(raw: list[str]) -> list[str]:
    """Trim, upper-case, de-duplicate (order-preserving), reject junk."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        # allow "99213, 99214 99215" pasted into one entry
        for tok in re.split(r"[\s,;]+", str(item)):
            code = tok.strip().upper()
            if not code:
                continue
            if not _CODE_RE.match(code):
                raise HTTPException(422, f"'{tok}' is not a valid CPT/HCPCS code.")
            if code not in seen:
                seen.add(code)
                out.append(code)
    if len(out) > MAX_CODES_PER_SET:
        raise HTTPException(422, f"Too many codes ({len(out)}); limit is {MAX_CODES_PER_SET}.")
    return out


@router.get("", response_model=list[CodeSetOut])
def list_code_sets(db: Session = Depends(get_db)) -> list[CodeSetOut]:
    return [CodeSetOut.from_row(cs) for cs in crud.list_code_sets(db)]


@router.post("", response_model=CodeSetOut)
def create_code_set(body: CodeSetIn, db: Session = Depends(get_db)) -> CodeSetOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    codes = clean_codes(body.codes)
    if not codes:
        raise HTTPException(422, "codes must contain at least one code")

    if crud.get_code_set_by_name(db, name):
        raise HTTPException(409, f"A code set named '{name}' already exists.")

    cs = crud.create_code_set(db, name=name, codes=codes)
    log.info("saved code set %r (%d codes)", name, len(codes))
    return CodeSetOut.from_row(cs)


@router.get("/{set_id}", response_model=CodeSetOut)
def get_code_set(set_id: str, db: Session = Depends(get_db)) -> CodeSetOut:
    cs = crud.get_code_set(db, set_id)
    if not cs:
        raise HTTPException(404, "Code set not found")
    return CodeSetOut.from_row(cs)


@router.put("/{set_id}", response_model=CodeSetOut)
def update_code_set(
    set_id: str, body: CodeSetUpdateIn, db: Session = Depends(get_db)
) -> CodeSetOut:
    if not crud.get_code_set(db, set_id):
        raise HTTPException(404, "Code set not found")

    name = body.name.strip() if body.name is not None else None
    if name is not None:
        if not name:
            raise HTTPException(422, "name cannot be blank")
        dup = crud.get_code_set_by_name(db, name)
        if dup and dup.id != set_id:
            raise HTTPException(409, f"A code set named '{name}' already exists.")

    codes = clean_codes(body.codes) if body.codes is not None else None
    if codes is not None and not codes:
        raise HTTPException(422, "codes must contain at least one code")

    cs = crud.update_code_set(db, set_id, name=name, codes=codes)
    return CodeSetOut.from_row(cs)


@router.delete("/{set_id}")
def delete_code_set(set_id: str, db: Session = Depends(get_db)) -> dict:
    if not crud.delete_code_set(db, set_id):
        raise HTTPException(404, "Code set not found")
    return {"ok": True}
