"""Pre-labelling with registry models, and the bake-off that picks one by measurement
(prompt §6 module 3).

Boxes a model proposes are stored with provenance `auto:<name>@<sha12>` and never enter a training
set (rule 2). They stay after a human confirms the frame, so the bake-off can compare every model
with what the human decided: precision and recall at IoU 0.5, on human-confirmed frames only.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from inno_count.detector import ModelLoadError, OnnxDetector
from inno_count.geometry import iou_matrix

from . import db, registry
from .explain import activity
from .projects import get_project, project_dir

EDGE_MARGIN_PX = 3.0
BAKEOFF_MIN_FRAMES = 100


class PrelabelError(Exception):
    """A problem the owner can understand and fix."""


def _detector(entry: dict[str, Any]) -> OnnxDetector:
    if entry["role"] != "detector":
        raise PrelabelError(f"“{entry['name']}” is not a detector.")
    try:
        return OnnxDetector(entry["path"])
    except ModelLoadError as exc:
        raise PrelabelError(str(exc)) from exc


def _class_map(slug: str, names: list[str]) -> dict[int, str]:
    """Model class index -> project class id, by id or display name; otherwise 'object'."""
    classes = get_project(slug)["classes"]
    lookup = {c["id"].lower(): c["id"] for c in classes} | {c["display_name"].lower(): c["id"] for c in classes}
    return {i: lookup.get(str(n).lower(), "object") for i, n in enumerate(names)}


def run_prelabel(slug: str, model_id: str, only_unreviewed: bool = False,
                 progress: Callable[[float, str], None] | None = None, image_ids: list[int] | None = None) -> dict[str, Any]:
    folder = project_dir(slug)
    entry = registry.get_model(model_id)
    det = _detector(entry)
    prov = registry.provenance(entry)
    cmap = _class_map(slug, det.names)
    import cv2

    with db.connect(folder) as con:
        q = "SELECT id, rel_path, width, height FROM media WHERE kind = 'image'"
        if only_unreviewed:
            q += " AND review IS NULL"
        rows = [dict(r) for r in con.execute(q + " ORDER BY id")]
    if image_ids is not None:
        wanted = set(image_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not rows:
        raise PrelabelError("There are no pictures to pre-label. Choose frames first.")
    started, boxes_total, ms = time.monotonic(), 0, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for k, r in enumerate(rows):
        data = np.frombuffer((folder / r["rel_path"]).read_bytes(), np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            continue
        t0 = time.perf_counter()
        found = det.detect(image)
        ms.append(1000 * (time.perf_counter() - t0))
        h, w = image.shape[:2]
        with db.connect(folder) as con:
            con.execute("DELETE FROM labels WHERE media_id = ? AND provenance = ?", (r["id"], prov))
            for d in found:
                trunc = d.x1 <= EDGE_MARGIN_PX or d.y1 <= EDGE_MARGIN_PX or d.x2 >= w - EDGE_MARGIN_PX or d.y2 >= h - EDGE_MARGIN_PX
                con.execute(
                    "INSERT INTO labels (media_id, class_id, bbox, provenance, created_at, score, truncated)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (r["id"], cmap.get(d.cls, "object"), json.dumps([round(d.x1, 1), round(d.y1, 1),
                     round(d.w, 1), round(d.h, 1)]), prov, now, round(d.score, 4), int(trunc)))
        boxes_total += len(found)
        if progress and k % 5 == 0:
            progress(k / len(rows), f"{k}/{len(rows)} صورة")
    result = {"model": entry["name"], "images": len(rows), "boxes": boxes_total,
              "ms_per_image": round(float(np.mean(ms)), 1) if ms else None, "provider": det.provider}
    activity.log(
        folder, "prelabel.run",
        f"اقترح «{entry['name']}» {boxes_total} صندوقًا على {len(rows)} صورة.",
        f"“{entry['name']}” proposed {boxes_total} boxes on {len(rows)} pictures.",
        why_ar="هذه اقتراحات فقط (مصدرها: موديل). لا تدخل التدريب حتى يؤكدها إنسان في شاشة المراجعة.",
        why_en="These are proposals only (source: a model). They enter training only after a human confirms them in review.",
        inputs={"model": entry["name"], "sha": entry["sha256"][:12], "only_unreviewed": only_unreviewed},
        numbers=result, duration_s=round(time.monotonic() - started, 1))
    return result


def _boxes(rows) -> np.ndarray:
    out = []
    for r in rows:
        x, y, w, h = json.loads(r["bbox"])
        out.append([x, y, x + w, y + h])
    return np.array(out, dtype=np.float64).reshape(-1, 4)


def score_model(slug: str, entry: dict[str, Any], iou: float = 0.5, conf: float | None = None) -> dict[str, Any]:
    """Precision / recall of one model's stored proposals against human-confirmed boxes."""
    folder = project_dir(slug)
    prov = registry.provenance(entry)
    conf = conf if conf is not None else float(entry.get("conf") or 0.25)
    tp = fp = fn = 0
    fp_on_empty = frames = 0
    with db.connect(folder) as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM media WHERE kind = 'image' AND review = 'confirmed'")]
        for mid in ids:
            human = con.execute("SELECT bbox FROM labels WHERE media_id = ? AND bbox IS NOT NULL AND provenance LIKE 'human:%'",
                                (mid,)).fetchall()
            auto = con.execute("SELECT bbox, score FROM labels WHERE media_id = ? AND provenance = ? AND score >= ?"
                               " ORDER BY score DESC", (mid, prov, conf)).fetchall()
            frames += 1
            hb, ab = _boxes(human), _boxes(auto)
            matched_h: set[int] = set()
            if len(hb) and len(ab):
                m = iou_matrix(ab, hb)
                for a in range(len(ab)):          # highest score first
                    cand = [(m[a, j], j) for j in range(len(hb)) if j not in matched_h and m[a, j] >= iou]
                    if cand:
                        matched_h.add(max(cand)[1])
                        tp += 1
                    else:
                        fp += 1
            else:
                fp += len(ab)
            fn += len(hb) - len(matched_h)
            if len(hb) == 0:
                fp_on_empty += len(ab)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {"model_id": entry["id"], "name": entry["name"], "licence": entry["licence"], "may_ship": entry["may_ship"],
            "frames": frames, "tp": tp, "fp": fp, "fn": fn, "fp_on_empty_frames": fp_on_empty,
            "precision": None if precision is None else round(precision, 4),
            "recall": None if recall is None else round(recall, 4),
            "f1": None if f1 is None else round(f1, 4), "conf": conf, "iou": iou}


def run_bakeoff(slug: str, model_ids: list[str], progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    folder = project_dir(slug)
    if not model_ids:
        raise PrelabelError("Choose at least one detector to compare.")
    with db.connect(folder) as con:
        confirmed = [r["id"] for r in con.execute("SELECT id FROM media WHERE kind = 'image' AND review = 'confirmed'")]
    if not confirmed:
        raise PrelabelError("Confirm some frames in review first: the comparison is against your boxes.")
    rows = []
    for i, mid in enumerate(model_ids):
        entry = registry.get_model(mid)
        if progress:
            progress(i / len(model_ids), f"تشغيل {entry['name']} على الصور المؤكدة")
        run_prelabel(slug, mid, image_ids=confirmed)   # make sure every confirmed frame has its proposals
        rows.append(score_model(slug, entry))
    rows.sort(key=lambda r: (r["f1"] or 0), reverse=True)
    enough = len(confirmed) >= BAKEOFF_MIN_FRAMES
    best = rows[0]
    activity.log(
        folder, "prelabel.bakeoff",
        f"مقارنة {len(rows)} موديل على {len(confirmed)} صورة مؤكدة: الأفضل «{best['name']}» "
        f"(دقة {best['precision']}، استرجاع {best['recall']})." + ("" if enough else " العدد أقل من 100 صورة: النتيجة مبدئية."),
        f"Bake-off of {len(rows)} models on {len(confirmed)} confirmed frames: best “{best['name']}” "
        f"(precision {best['precision']}, recall {best['recall']})." + ("" if enough else " Fewer than 100 frames: provisional."),
        why_ar="الموديل المساعد يُختار بالقياس على تسميتك، لا بالاسم.",
        why_en="The helper model is chosen by measurement against your labels, never by name.",
        numbers={"frames": len(confirmed), "models": len(rows)}, outputs={"ranking": [r["name"] for r in rows]})
    return {"frames": len(confirmed), "enough": enough, "min_frames": BAKEOFF_MIN_FRAMES, "models": rows}
