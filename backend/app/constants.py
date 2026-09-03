"""
Shared constants: the extensible payer list and the target CPT/HCPCS code
set. The code set is imported straight from the existing `extract_tic_all.py`
so there is a single source of truth (see pipeline/extract.py for how it is
loaded without executing the script's __main__ block).
"""
from __future__ import annotations

# --- Insurance companies -----------------------------------------------------
# `key` is what gets stored on the job + used as the on-disk folder name.
# `label` is what the UI shows. Add rows here to extend the list.
INSURANCE_COMPANIES: list[dict[str, str]] = [
    {"key": "uhc", "label": "UnitedHealthcare (UHC)"},
    {"key": "aetna", "label": "Aetna"},
    {"key": "mvp", "label": "MVP Health Care"},
    {"key": "anthem", "label": "Anthem"},
]

INSURANCE_COMPANY_KEYS = {c["key"] for c in INSURANCE_COMPANIES}


def company_label(key: str) -> str:
    for c in INSURANCE_COMPANIES:
        if c["key"] == key:
            return c["label"]
    return key


# --- Pipeline stages (shown in the UI progress bar) -------------------------
STAGE_QUEUED = "Queued"
STAGE_PREPARING = "Preparing"
STAGE_BUILDING_LOOKUP = "Building provider lookup"
STAGE_EXTRACTING = "Extracting rates"
STAGE_ENRICHING = "Enriching NPIs"
STAGE_PACKAGING = "Packaging"
STAGE_DONE = "Completed"

# Accepted upload extensions
ACCEPTED_EXTENSIONS = (".json.gz", ".json")
