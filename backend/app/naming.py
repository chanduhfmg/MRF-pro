"""
Human-readable download filenames.

    {PAYER}_{plan}_{TIN|NPI+id | ALL}_{YYYYMMDD}_{codeset}.xlsx

    UHC_Oxford-Metro-27_NPI1013988054_20260914_Ophthalmology.xlsx
    CIGNA_PathWell-PPO_TIN473943680_20260914_default.xlsx
    ANTHEM_Blue-Access_ALL_20260914_custom14.xlsx

Every token is deliberately short: the point is that someone looking at a
folder of these can tell payer, plan, provider, when, and which code list
without opening anything. Payer plan names are long and full of legal
boilerplate ("Oxford Health Insurance, Inc. Insurer Metro Network 27"), so
the plan token drops the boilerplate words and keeps the first three that
carry meaning.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# Words that appear in nearly every plan name and identify nothing.
_NOISE = {
    "inc", "llc", "lp", "ltd", "co", "corp", "corporation", "company",
    "insurer", "insurance", "third", "party", "administrator", "tpa",
    "health", "healthcare", "care", "plan", "plans", "group", "benefit",
    "benefits", "of", "the", "and", "in", "network", "rates", "file",
    "in-network", "json", "gz",
}
_LEADING_DATE = re.compile(r"^\d{4}[-_]\d{2}[-_]\d{2}[-_]?")


def short_plan(name: str | None, *, fallback: str = "file", max_len: int = 26) -> str:
    """'Oxford Health Insurance, Inc. Insurer Metro Network 27' -> 'Oxford-Metro-27'."""
    raw = _LEADING_DATE.sub("", (name or "").strip())
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", raw) if t]
    keep = [t for t in tokens if t.lower() not in _NOISE]
    if not keep:
        keep = tokens[:3]
    out = "-".join(keep[:3])[:max_len].rstrip("-")
    return out or fallback


def short_codeset(name: str | None, code_count: int, *, is_default: bool) -> str:
    if is_default:
        return "default"
    if name:
        s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:20].rstrip("-")
        if s:
            return s
    return f"custom{code_count}"


def csv_part_prefix(job, codes: list | None) -> str:
    """Prefix for the loose CSV parts inside a job's zip (part_0001.csv ->
    AETNA_Ophthalmology_part_0001.csv), so a file still says which payer and
    code set it came from once it's been extracted out of the zip and the
    zip's own filename is gone."""
    payer = re.sub(r"[^A-Za-z0-9]", "", (job.insurance_company or "payer").upper())
    codeset = short_codeset(getattr(job, "code_set_name", None), len(codes or []), is_default=not codes)
    return f"{payer}_{codeset}_"


def download_basename(job, ext: str) -> str:
    """Filename for a job's download. `job` is an ExtractionJob row."""
    payer = re.sub(r"[^A-Za-z0-9]", "", (job.insurance_company or "payer").upper())

    plan_source = job.plan_name or Path(job.source_filename or "").name
    for suffix in (".json.gz", ".json", ".gz"):
        if plan_source.lower().endswith(suffix):
            plan_source = plan_source[: -len(suffix)]
    plan = short_plan(plan_source)

    target: str | None = None
    if job.target_type and job.target_value:
        target = (
            f"{job.target_type.upper()}{re.sub(r'[^0-9]', '', str(job.target_value))}"
            if job.filter_to_target
            else "ALL"
        )

    when = job.completed_at or job.created_at or datetime.utcnow()
    date = when.strftime("%Y%m%d")

    codes: list = []
    if getattr(job, "target_codes", None):
        try:
            parsed = json.loads(job.target_codes)
            codes = parsed if isinstance(parsed, list) else []
        except Exception:
            codes = []
    codeset = short_codeset(getattr(job, "code_set_name", None), len(codes), is_default=not codes)

    parts = [payer, plan] + ([target] if target else []) + [date, codeset]
    name = "_".join(parts)
    name = re.sub(r"[^A-Za-z0-9_\-]+", "", name).strip("_") or f"extraction_{job.id[:8]}"
    return f"{name}.{ext}"
