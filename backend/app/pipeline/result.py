from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PipelineResult:
    record_count: int
    output_file_count: int
    zip_path: Path
    zip_size_bytes: int
