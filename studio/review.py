"""Human review of boxes (prompt §6 module 5). The only way a box becomes `human:<user>`.

- The queue puts first the frames the models are least sure about: many boxes with a middling
  score, then frames where models disagree in number, then the rest.
- Confirming a frame replaces its human boxes with what the reviewer drew; the models' proposals
  stay, for the bake-off. Confirming with no box marks the frame as a negative (nothing to count).
- A box cut by the frame edge is kept and labelled with `truncated`, so the detector learns to see
  objects entering the frame; the counter, not the label, keeps it out of count and size.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import current_user
from .projects import get_project, project_dir

UNSURE = (0.2, 0.6)


class ReviewError(Exception):
    """A problem the owner can understand and fix."""


def class_ids(slug: str) -> list[str]:
    """Project classes, plus "object": the Counting Engine's detector is single-class, so a box
    may be confirmed as just "an object" and its type left to the Classification Engine."""
    return [c["id"] for c in get_project(slug)["classes"]] + ["object"]


def stats(slug: str) -> dict[str, Any]:
    with db.connect(project_dir(slug)) as con:
        total = con.execute("SELECT COUNT(*) FROM media WHERE kind = 'image'").fetchone()[0]
        confirmed = con.execute("SELECT COUNT(*) FROM media WHERE kind = 'image' AND review = 'confirmed'").fetchone()[0]
        negatives = con.execute(
            "SELECT COUNT(*) FROM media m WHERE kind = 'image' AND review = 'confirmed' AND NOT EXISTS"
            " (SELECT 1 FROM labels l WHERE l.media_id = m.id AND l.bbox IS NOT NULL AND l.provenance LIKE 'human:%')").fetchone()[0]
        per_class = {r["class_id"]: r["n"] for r in con.execute(
            "SELECT class_id, COUNT(*) AS n FROM labels WHERE bbox IS NOT NULL AND provenance LIKE 'human:%'"
            " AND truncated = 0 GROUP BY class_id")}
        truncated = con.execute("SELECT COUNT(*) FROM labels WHERE bbox IS NOT NULL AND provenance LIKE 'human:%'"
                                " AND truncated = 1").fetchone()[0]
        with_proposals = con.execute(
            "SELECT COUNT(DISTINCT media_id) FROM labels WHERE provenance LIKE 'auto:%'").fetchone()[0]
    return {"images": total, "confirmed": confirmed, "negatives": negatives, "remaining": total - confirmed,
            "boxes_per_class": per_class, "truncated_boxes": truncated, "with_proposals": with_proposals}


def queue(slug: str, limit: int = 500) -> list[int]:
    lo, hi = UNSURE
    with db.connect(project_dir(slug)) as con:
        rows = con.execute(
            f"""SELECT m.id,
                   (SELECT COUNT(*) FROM labels l WHERE l.media_id = m.id AND l.provenance LIKE 'auto:%'
                        AND l.score BETWEEN {lo} AND {hi}) AS unsure,
                   (SELECT COUNT(DISTINCT l.provenance) FROM labels l WHERE l.media_id = m.id AND l.provenance LIKE 'auto:%') AS models,
                   (SELECT MAX(c) - MIN(c) FROM (SELECT COUNT(*) AS c FROM labels l WHERE l.media_id = m.id
                        AND l.provenance LIKE 'auto:%' AND l.score > {hi} GROUP BY l.provenance)) AS spread
               FROM media m WHERE m.kind = 'image' AND m.review IS NULL
               ORDER BY unsure DESC, COALESCE(spread, 0) DESC, models DESC, m.id
               LIMIT ?""", (limit,)).fetchall()
    return [r["id"] for r in rows]


def get_frame(slug: str, media_id: int) -> dict[str, Any]:
    with db.connect(project_dir(slug)) as con:
        m = con.execute("SELECT * FROM media WHERE id = ? AND kind = 'image'", (media_id,)).fetchone()
        if m is None:
            raise ReviewError(f"Picture #{media_id} not found.")
        labels = [dict(r) for r in con.execute("SELECT * FROM labels WHERE media_id = ? ORDER BY score DESC", (media_id,))]
    human, proposals, whole = [], {}, []
    for lab in labels:
        if lab["bbox"] is None:
            whole.append({"class_id": lab["class_id"], "provenance": lab["provenance"]})
            continue
        x, y, w, h = json.loads(lab["bbox"])
        box = {"x": x, "y": y, "w": w, "h": h, "class_id": lab["class_id"], "truncated": bool(lab["truncated"]),
               "score": lab["score"]}
        if lab["provenance"].startswith("human:"):
            human.append(box)
        else:
            proposals.setdefault(lab["provenance"], []).append(box)
    out = dict(m)
    out.update(human=human, proposals=proposals, whole_image_labels=whole, blind=is_blind(media_id))
    return out


BLIND_SHARE = 0.05


def is_blind(media_id: int) -> bool:
    """A fixed 5 % of frames are reviewed without the models' proposals, to measure how often
    bulk confirmation follows a wrong suggestion (prompt §6 module 5). Fixed per frame, so a
    reload does not change it."""
    return (media_id * 2654435761 % 2**32) / 2**32 < BLIND_SHARE


def blind_audit(slug: str) -> dict[str, Any]:
    """Compare box counts on blind and normal frames against the best model's proposals."""
    with db.connect(project_dir(slug)) as con:
        rows = con.execute(
            "SELECT m.id, m.review_blind,"
            " (SELECT COUNT(*) FROM labels l WHERE l.media_id = m.id AND l.bbox IS NOT NULL AND l.provenance LIKE 'human:%') AS human,"
            " (SELECT COUNT(*) FROM labels l WHERE l.media_id = m.id AND l.provenance LIKE 'auto:%' AND l.score >= 0.25) AS auto"
            " FROM media m WHERE m.kind = 'image' AND m.review = 'confirmed'").fetchall()
    out = {}
    for blind in (0, 1):
        sel = [r for r in rows if (r["review_blind"] or 0) == blind and r["auto"] > 0]
        same = sum(1 for r in sel if r["human"] == r["auto"])
        out["blind" if blind else "shown"] = {"frames": len(sel), "same_count_as_model": same,
                                              "share": round(same / len(sel), 3) if sel else None}
    return out


def confirm(slug: str, media_id: int, boxes: list[dict[str, Any]], blind: bool = False) -> dict[str, Any]:
    folder = project_dir(slug)
    allowed = set(class_ids(slug))
    with db.connect(folder) as con:
        m = con.execute("SELECT width, height FROM media WHERE id = ? AND kind = 'image'", (media_id,)).fetchone()
        if m is None:
            raise ReviewError(f"Picture #{media_id} not found.")
        clean = []
        for b in boxes:
            x, y, w, h = (float(b[k]) for k in ("x", "y", "w", "h"))
            x1, y1 = max(0.0, x), max(0.0, y)
            x2, y2 = min(float(m["width"]), x + w), min(float(m["height"]), y + h)
            if x2 - x1 < 3 or y2 - y1 < 3:
                raise ReviewError("A box is too small or outside the picture.")
            cls = b.get("class_id") or "object"
            if cls not in allowed:
                raise ReviewError(f"Unknown class: {cls}")
            clean.append(([round(x1, 1), round(y1, 1), round(x2 - x1, 1), round(y2 - y1, 1)], cls, int(bool(b.get("truncated")))))
        user = current_user()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con.execute("DELETE FROM labels WHERE media_id = ? AND bbox IS NOT NULL AND provenance LIKE 'human:%'", (media_id,))
        for bbox, cls, trunc in clean:
            con.execute("INSERT INTO labels (media_id, class_id, bbox, provenance, created_at, truncated) VALUES (?,?,?,?,?,?)",
                        (media_id, cls, json.dumps(bbox), f"human:{user}", now, trunc))
        con.execute("UPDATE media SET review = 'confirmed', reviewed_by = ?, reviewed_at = ?, review_blind = ? WHERE id = ?",
                    (user, now, int(blind), media_id))
    return get_frame(slug, media_id)


def reopen(slug: str, media_id: int) -> dict[str, Any]:
    with db.connect(project_dir(slug)) as con:
        con.execute("UPDATE media SET review = NULL WHERE id = ? AND kind = 'image'", (media_id,))
    return get_frame(slug, media_id)
