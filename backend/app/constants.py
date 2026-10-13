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
#
# Single source of truth is `payers.PAYER_SOURCES` - the same registry that
# holds each payer's hardcoded MRF index URL - so a payer can never appear in
# the dropdown without a place to fetch its files from.
from .payers import PAYER_SOURCES  # noqa: E402

INSURANCE_COMPANIES: list[dict[str, str]] = [
    {"key": s.key, "label": s.label} for s in PAYER_SOURCES.values()
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
STAGE_DOWNLOADING = "Downloading MRF"
STAGE_BUILDING_LOOKUP = "Building provider lookup"
STAGE_EXTRACTING = "Extracting rates"
STAGE_ENRICHING = "Enriching NPIs"
STAGE_PACKAGING = "Packaging"
STAGE_DONE = "Completed"

# --- Provider-search stages -------------------------------------------------
SEARCH_STAGE_QUEUED = "Queued"
SEARCH_STAGE_RESOLVING = "Resolving payer index"
SEARCH_STAGE_LISTING = "Reading table of contents"
SEARCH_STAGE_SCANNING = "Scanning in-network files"
SEARCH_STAGE_DONE = "Completed"

SEARCH_TYPES = ("npi", "tin")

# Accepted upload extensions
ACCEPTED_EXTENSIONS = (".json.gz", ".json")
