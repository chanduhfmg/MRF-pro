from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PipelineResult:
    record_count: int
    output_file_count: int
    zip_path: Path
    zip_size_bytes: int
    # None when the output exceeds what one Excel sheet can hold
    xlsx_path: Path | None = None
    # How many of the requested CPT/HCPCS codes actually appeared in the
    # file (out of how many were requested), and which ones. A 0 record
    # count is ambiguous on its own - this says whether that is because the
    # codes never showed up at all, or because they did but nobody billing
    # them was the searched provider.
    requested_code_count: int | None = None
    matched_code_count: int | None = None
    matched_codes: list[str] | None = None
