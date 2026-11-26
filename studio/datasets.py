"""Dataset snapshots for training (P4).

A snapshot is a frozen copy of the human-confirmed pictures and their boxes, written in the COCO
layout RF-DETR reads (`train/`, `valid/`, `test/`, each with `_annotations.coco.json`). It never
changes after it is written; training always names the snapshot it used.

Rules (prompt §6 modules 5–6):
- Only boxes a person confirmed (`human:` provenance) are used. Proposals from a model or from
  Claude are never trained on.
- Splits are by **time block**, not by random frame: frames a few seconds apart look alike, so a
  random split puts near-copies in train and valid and the score lies (audit §1 item 2). A block is
  `block_s` seconds of one video; a whole block goes to one split.
- Frames near a truth clip (± the sampling buffer) are left out, so the scoreboard stays unseen.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db
from .explain import activity
from .projects import get_project, project_dir
from .sample import truth_ranges

SPLITS = ("train", "valid", "test")
DEFAULT_FRACTIONS = (0.7, 0.15, 0.15)
MIN_IMAGES = 30


class DatasetError(Exception):
    """A problem the owner can understand and fix."""


def _snap_root(slug: str) -> Path:
    return project_dir(slug) / "datasets"


def _block_key(row: Any, block_s: float) -> str:
    if row["source_media_id"] is not None and row["source_t"] is not None:
        return f"v{row['source_media_id']}:{int(row['source_t'] // block_s)}"
    return f"i{row['id']}"   # an uploaded picture is its own block


def _in_truth(row: Any, ranges: dict[int, list[tuple[float, float]]]) -> bool:
    if row["source_media_id"] is None or row["source_t"] is None:
        return False
    return any(a <= row["source_t"] <= b for a, b in ranges.get(row["source_media_id"], []))


def assign_blocks(sizes: dict[str, int], fractions: tuple[float, float, float], seed: int) -> dict[str, str]:
    """Blocks -> split. Deterministic for a seed; every split gets at least one block when there are 3+."""
    order = sorted(sizes, key=lambda k: hashlib.sha256(f"{seed}:{k}".encode()).hexdigest())
    total = sum(sizes.values())
    target = {s: f * total for s, f in zip(SPLITS, fractions)}
    have = dict.fromkeys(SPLITS, 0)
    out: dict[str, str] = {}
    # Seed valid and test with one block each, then fill whichever split is furthest below its target.
    for split, key in zip(("valid", "test"), order[:2] if len(order) >= 3 else []):
        out[key] = split
        have[split] += sizes[key]
    for key in order:
        if key in out:
            continue
        split = max(SPLITS, key=lambda s: (target[s] - have[s]) / max(target[s], 1e-9))
        out[key] = split
        have[split] += sizes[key]
    return out


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_snapshot(slug: str, block_s: float = 600.0, fractions: tuple[float, float, float] = DEFAULT_FRACTIONS,
                   seed: int = 0, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    if block_s <= 0 or len(fractions) != 3 or min(fractions) < 0 or fractions[1] <= 0 or abs(sum(fractions) - 1) > 1e-6:
        raise DatasetError("Block length must be positive and the three split shares must add up to 1 (valid > 0).")
    folder = project_dir(slug)
    meta = get_project(slug)
    classes = [c["id"] for c in meta["classes"]]
    if not classes:
        raise DatasetError("Add at least one class first (Classes tab).")
    cat = {c: i for i, c in enumerate(classes)}
    ranges = truth_ranges(slug)
    with db.connect(folder) as con:
        media = con.execute(
            "SELECT * FROM media WHERE kind = 'image' AND review = 'confirmed' ORDER BY id").fetchall()
        labels = con.execute(
            "SELECT media_id, class_id, bbox FROM labels WHERE bbox IS NOT NULL AND provenance LIKE 'human:%' "
            "ORDER BY id").fetchall()
    boxes: dict[int, list[tuple[str, list[float]]]] = {}
    for r in labels:
        boxes.setdefault(r["media_id"], []).append((r["class_id"], json.loads(r["bbox"])))
    skipped_truth = [m["id"] for m in media if _in_truth(m, ranges)]
    rows = [m for m in media if m["id"] not in set(skipped_truth)]
    unknown = sorted({c for m in rows for c, _ in boxes.get(m["id"], []) if c not in cat})
    if unknown:
        raise DatasetError(f"Some confirmed boxes use classes that no longer exist: {', '.join(unknown)}.")
    if len(rows) < MIN_IMAGES:
        raise DatasetError(f"Only {len(rows)} confirmed pictures can be used; at least {MIN_IMAGES} are needed "
                           "(Label tab: confirm more pictures).")
    blocks: dict[str, list[Any]] = {}
    for m in rows:
        blocks.setdefault(_block_key(m, block_s), []).append(m)
    if len(blocks) < 3:
        raise DatasetError(f"The pictures come from only {len(blocks)} time block(s) of {block_s:.0f} s; at least 3 "
                           "are needed so valid and test are not copies of train. Sample from more of the footage "
                           "or use shorter blocks.")
    split_of = assign_blocks({k: len(v) for k, v in blocks.items()}, fractions, seed)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_id = f"ds-{stamp}"
    out = _snap_root(slug) / snap_id
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    categories = [{"id": i, "name": c, "supercategory": "none"} for c, i in cat.items()]
    coco = {s: {"info": {"description": f"{meta['name']} {snap_id} {s}"}, "images": [], "annotations": [],
                "categories": categories} for s in SPLITS}
    counts = {s: {"images": 0, "boxes": 0, "empty": 0, "blocks": 0} for s in SPLITS}
    for key, split in split_of.items():
        counts[split]["blocks"] += 1
    ann_id = 1
    done = 0
    for key, members in blocks.items():
        split = split_of[key]
        (tmp / split).mkdir(parents=True, exist_ok=True)
        for m in members:
            src = folder / m["rel_path"] if m["rel_path"] else Path(m["linked_path"])
            if not src.is_file():
                raise DatasetError(f"Picture #{m['id']} is not at its path any more: {src}")
            name = f"{m['id']:06d}{src.suffix.lower() or '.jpg'}"
            _link_or_copy(src, tmp / split / name)
            coco[split]["images"].append({"id": m["id"], "file_name": name, "width": m["width"], "height": m["height"]})
            mine = boxes.get(m["id"], [])
            for cls, (x, y, w, h) in mine:
                coco[split]["annotations"].append({"id": ann_id, "image_id": m["id"], "category_id": cat[cls],
                                                   "bbox": [x, y, w, h], "area": round(w * h, 1), "iscrowd": 0})
                ann_id += 1
            counts[split]["images"] += 1
            counts[split]["boxes"] += len(mine)
            counts[split]["empty"] += int(not mine)
            done += 1
            if progress and done % 20 == 0:
                progress(done / len(rows), f"نسخ الصور: {done}/{len(rows)}")
    hashes = {}
    for s in SPLITS:
        (tmp / s).mkdir(parents=True, exist_ok=True)
        data = json.dumps(coco[s], ensure_ascii=False, indent=1)
        (tmp / s / "_annotations.coco.json").write_text(data, encoding="utf-8")
        hashes[s] = hashlib.sha256(data.encode("utf-8")).hexdigest()
    manifest = {
        "id": snap_id, "project": slug, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "format": "coco", "classes": classes, "block_s": block_s, "fractions": list(fractions), "seed": seed,
        "counts": counts, "annotation_sha256": hashes, "label_rule": "human: provenance only",
        "left_out_near_truth": len(skipped_truth),
        "blocks": {k: split_of[k] for k in sorted(split_of)},
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    activity.log(folder, "dataset.snapshot",
                 f"جُهّزت نسخة بيانات للتدريب {snap_id}: {counts['train']['images']} تدريب، "
                 f"{counts['valid']['images']} تحقق، {counts['test']['images']} اختبار.",
                 f"Dataset snapshot {snap_id} written.",
                 why_ar="التدريب يتم على صناديق أكّدها إنسان فقط، والتقسيم بكتل زمنية حتى لا تتكرر نفس اللقطة "
                        "في التدريب والتحقق فيبان الموديل أحسن من حقيقته.",
                 why_en="Only human-confirmed boxes, split by time block so valid is not a copy of train.",
                 outputs={"id": snap_id, "counts": counts, "left_out_near_truth": len(skipped_truth)})
    return manifest


def list_snapshots(slug: str) -> list[dict[str, Any]]:
    root = _snap_root(slug)
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if d.is_dir() and not d.name.endswith(".partial") and mf.exists():
            out.append(json.loads(mf.read_text(encoding="utf-8")))
    return out


def snapshot_dir(slug: str, snap_id: str) -> Path:
    if not snap_id.startswith("ds-") or "/" in snap_id or "\\" in snap_id or ".." in snap_id:
        raise DatasetError("Unknown dataset snapshot.")
    d = _snap_root(slug) / snap_id
    if not (d / "manifest.json").exists():
        raise DatasetError(f"Dataset snapshot {snap_id} not found.")
    return d
