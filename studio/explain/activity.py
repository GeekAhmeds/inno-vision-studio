"""Activity log: what happened, why, and with what numbers (prompt §5.6).

One JSON object per line in `<project>/activity.jsonl`. Append-only. Every figure shown in an
explanation or a report must come from here, never from a language model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import current_user

FILE = "activity.jsonl"


def log(
    project_dir: Path,
    step: str,
    ar: str,
    en: str,
    *,
    why_ar: str = "",
    why_en: str = "",
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    numbers: dict[str, Any] | None = None,
    duration_s: float | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "step": step,
        "user": current_user(),
        "ar": ar,
        "en": en,
        "why_ar": why_ar,
        "why_en": why_en,
        "inputs": inputs or {},
        "outputs": outputs or {},
        "numbers": numbers or {},
        "duration_s": duration_s,
        "cost_usd": cost_usd,
    }
    with (project_dir / FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(project_dir: Path, limit: int | None = None) -> list[dict[str, Any]]:
    path = project_dir / FILE
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    entries.reverse()  # newest first
    return entries[:limit] if limit else entries
