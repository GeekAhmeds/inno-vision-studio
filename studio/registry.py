"""Helper-model registry, shared by every project in the workspace (prompt §5.3).

An entry is a model file the studio can run, with its role, its licence, and whether it may ship to
a client machine. Models are linked in place (weights stay where they are) and hashed.

Licence rule (prompt rule 9, decision D6): while the workspace policy is "permissive_only", an entry
whose licence is copyleft (AGPL / GPL) can help in the studio — pre-labelling, comparing — but can
never be chosen for anything that ships (`require_shippable`).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from inno_count.detector import file_sha256, load_sidecar

from .config import workspace_dir

ROLES = {
    "detector": {"ar": "كاشف يرسم صناديق (تسمية مبدئية / عدّ)", "en": "Detector (draws boxes)"},
    "classifier": {"ar": "مصنِّف يحكم على الصورة المقصوصة", "en": "Classifier (judges a crop)"},
    "embedder": {"ar": "يجمع الصور المتشابهة", "en": "Embedder (groups similar images)"},
}
COPYLEFT = re.compile(r"\b(A?GPL|LGPL)", re.IGNORECASE)
POLICIES = ("permissive_only", "commercial_licence")


class RegistryError(Exception):
    """A problem the owner can understand and fix."""


def _file() -> Path:
    return workspace_dir() / "models" / "registry.json"


def _load() -> dict[str, Any]:
    path = _file()
    if not path.exists():
        return {"policy": "permissive_only", "models": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data: dict[str, Any]) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def policy() -> str:
    return _load()["policy"]


def set_policy(value: str) -> str:
    if value not in POLICIES:
        raise RegistryError(f"Policy must be one of {POLICIES}.")
    data = _load()
    data["policy"] = value
    _save(data)
    return value


def is_copyleft(licence: str) -> bool:
    return bool(COPYLEFT.search(licence or ""))


def _with_status(entry: dict[str, Any], pol: str) -> dict[str, Any]:
    out = dict(entry)
    out["copyleft"] = is_copyleft(entry["licence"])
    out["may_ship"] = not out["copyleft"] or pol == "commercial_licence"
    out["missing"] = not Path(entry["path"]).is_file()
    return out


def list_models(role: str | None = None) -> list[dict[str, Any]]:
    data = _load()
    return [_with_status(m, data["policy"]) for m in data["models"] if role is None or m["role"] == role]


def get_model(model_id: str) -> dict[str, Any]:
    data = _load()
    for m in data["models"]:
        if m["id"] == model_id:
            return _with_status(m, data["policy"])
    raise RegistryError(f"Model not found in the registry: {model_id}")


def add_model(path: str | Path, name: str, role: str = "detector", licence: str = "", note: str = "") -> dict[str, Any]:
    path = Path(path).expanduser()
    if not path.is_file():
        raise RegistryError(f"Model file not found: {path}")
    if path.suffix.lower() != ".onnx":
        raise RegistryError("Only ONNX models (.onnx) can be added for now. Export the model to ONNX first.")
    if role not in ROLES:
        raise RegistryError(f"Unknown role: {role}")
    if not licence.strip():
        raise RegistryError("Write the model's licence (for example: Apache-2.0, MIT, AGPL-3.0, own model).")
    sha = file_sha256(path)
    data = _load()
    for m in data["models"]:
        if m["sha256"] == sha and m["role"] == role:
            raise RegistryError(f"This model is already registered as “{m['name']}”.")
    side = load_sidecar(path)
    entry = {
        "id": f"m-{uuid.uuid4().hex[:8]}",
        "name": name.strip() or path.stem,
        "role": role,
        "path": str(path.resolve()),
        "sha256": sha,
        "licence": licence.strip(),
        "note": note.strip(),
        "names": side.get("names") or [],
        "imgsz": side.get("imgsz"),
        "conf": side.get("conf"),
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["models"].append(entry)
    _save(data)
    return _with_status(entry, data["policy"])


def remove_model(model_id: str) -> None:
    data = _load()
    before = len(data["models"])
    data["models"] = [m for m in data["models"] if m["id"] != model_id]
    if len(data["models"]) == before:
        raise RegistryError(f"Model not found in the registry: {model_id}")
    _save(data)


def require_shippable(model_id: str) -> dict[str, Any]:
    """Call before anything that ships to a client machine (export, publish)."""
    entry = get_model(model_id)
    if not entry["may_ship"]:
        raise RegistryError(
            f"“{entry['name']}” is licensed {entry['licence']} and the workspace policy is “permissive only”. "
            "It may help with labelling here, but it cannot ship to a client. (Decision D6.)")
    return entry


def provenance(entry: dict[str, Any]) -> str:
    """Label provenance for boxes a registry model proposed: auto:<name>@<sha12>."""
    return f"auto:{entry['name']}@{entry['sha256'][:12]}"
