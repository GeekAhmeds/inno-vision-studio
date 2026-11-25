"""Truth: human counts of short clips, the only scoreboard (prompt §6 module 8, acceptance test 12).

- A truth clip is a time range of a registered video, tagged with a condition (busy, piled, night…).
- A human plays it and taps once per object crossing the line, with its type; tapping ON the object
  also records where it crossed, which lets the harness tell a double count from a miss in a pile.
- Every clip is counted twice. The two counts are matched; any disagreement blocks the clip from
  the harness until the owner decides each case. The decided list is the clip's final truth.
- A final clip can be locked; a locked clip never changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..config import current_user
from ..explain import activity
from ..harness.match import match
from ..projects import get_project, project_dir

CONDITIONS: dict[str, str] = {
    "normal": "عادي",
    "busy": "زحمة",
    "piled": "متراكم / ملتصق",
    "hands": "أيدي فوق السير",
    "glare": "انعكاس ضوء",
    "night": "ليل",
    "empty": "سير فاضي",
    "other": "أخرى",
}
AGREE_WINDOW_S = 1.0


class TruthError(Exception):
    """A problem the owner can understand and fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip_row(con, clip_id: int):
    row = con.execute("SELECT * FROM truth_clips WHERE id = ?", (clip_id,)).fetchone()
    if row is None:
        raise TruthError(f"Truth clip #{clip_id} not found.")
    return row


def create_clip(slug: str, media_id: int, start_s: float, end_s: float, condition: str, note: str = "") -> dict[str, Any]:
    folder = project_dir(slug)
    if condition not in CONDITIONS:
        raise TruthError(f"Unknown condition: {condition}")
    start_s, end_s = float(start_s), float(end_s)
    if start_s < 0 or end_s <= start_s:
        raise TruthError("The clip needs a start before its end.")
    if end_s - start_s > 15 * 60:
        raise TruthError("Keep truth clips short (2–5 minutes, at most 15): a tired counter makes mistakes.")
    with db.connect(folder) as con:
        media = con.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if media is None or media["kind"] != "video":
            raise TruthError("Choose a video registered in this project.")
        if media["duration_s"] and end_s > media["duration_s"] + 0.5:
            raise TruthError(f"The video is only {media['duration_s']:.1f} s long.")
        cur = con.execute(
            "INSERT INTO truth_clips (media_id, start_s, end_s, condition, note, created_at) VALUES (?,?,?,?,?,?)",
            (media_id, start_s, end_s, condition, note, _now()))
        clip_id = cur.lastrowid
    activity.log(folder, "truth.clip",
                 f"أُنشئ مقطع مسطرة #{clip_id} ({CONDITIONS[condition]}) من {start_s:.0f} إلى {end_s:.0f} ث.",
                 f"Truth clip #{clip_id} created ({condition}), {start_s:.0f}–{end_s:.0f} s.",
                 why_ar="المسطرة هي العدّ البشري الذي يُقاس عليه أي موديل. هذا الجزء من الفيديو لن يُستخدم في التدريب.",
                 why_en="Truth is the human count every model is measured against. This time range is kept out of training.",
                 outputs={"clip_id": clip_id, "media_id": media_id})
    return get_clip(slug, clip_id)


def get_clip(slug: str, clip_id: int) -> dict[str, Any]:
    with db.connect(project_dir(slug)) as con:
        row = dict(_clip_row(con, clip_id))
        counts = [dict(r) for r in con.execute(
            "SELECT id, counter, finished, created_at, taps FROM truth_counts WHERE clip_id = ? ORDER BY id", (clip_id,))]
        media = dict(con.execute("SELECT * FROM media WHERE id = ?", (row["media_id"],)).fetchone())
    for c in counts:
        c["taps"] = json.loads(c["taps"])
        c["n"] = len(c["taps"])
    row["counts"] = counts
    row["final_taps"] = json.loads(row["final_taps"]) if row["final_taps"] else None
    row["media"] = media
    row["status"] = _status(row)
    return row


def _status(clip: dict[str, Any]) -> str:
    if clip["final_taps"] is not None:
        return "locked" if clip["locked"] else "final"
    finished = [c for c in clip["counts"] if c["finished"]]
    if len(finished) < 2:
        return f"needs_count_{len(finished) + 1}"
    return "needs_reconcile"


def list_clips(slug: str) -> list[dict[str, Any]]:
    with db.connect(project_dir(slug)) as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM truth_clips ORDER BY id")]
    return [get_clip(slug, i) for i in ids]


def delete_clip(slug: str, clip_id: int) -> None:
    clip = get_clip(slug, clip_id)
    if clip["locked"]:
        raise TruthError("This truth clip is locked and cannot be deleted.")
    with db.connect(project_dir(slug)) as con:
        con.execute("DELETE FROM truth_clips WHERE id = ?", (clip_id,))


def start_count(slug: str, clip_id: int) -> dict[str, Any]:
    clip = get_clip(slug, clip_id)
    if clip["final_taps"] is not None:
        raise TruthError("This clip already has its final truth.")
    with db.connect(project_dir(slug)) as con:
        cur = con.execute("INSERT INTO truth_counts (clip_id, counter, created_at) VALUES (?,?,?)",
                          (clip_id, current_user(), _now()))
    return {"id": cur.lastrowid, "clip_id": clip_id, "counter": current_user(), "taps": [], "finished": 0}


def _clean_taps(taps: list[dict[str, Any]], clip: dict[str, Any], classes: set[str]) -> list[dict[str, Any]]:
    out = []
    for tap in taps:
        t = float(tap["t"])
        if not clip["start_s"] - 0.5 <= t <= clip["end_s"] + 0.5:
            raise TruthError(f"A tap at {t:.2f} s is outside the clip.")
        cls = tap.get("class_id") or "unknown"
        if cls != "unknown" and cls not in classes:
            raise TruthError(f"Unknown class: {cls}")
        x, y = tap.get("x"), tap.get("y")
        out.append({"t": round(t, 3), "class_id": cls,
                    "x": None if x is None else round(float(x), 1), "y": None if y is None else round(float(y), 1)})
    return sorted(out, key=lambda tap: tap["t"])


def save_count(slug: str, count_id: int, taps: list[dict[str, Any]], finished: bool) -> dict[str, Any]:
    folder = project_dir(slug)
    with db.connect(folder) as con:
        row = con.execute("SELECT * FROM truth_counts WHERE id = ?", (count_id,)).fetchone()
        if row is None:
            raise TruthError(f"Count #{count_id} not found.")
    clip = get_clip(slug, row["clip_id"])
    if clip["final_taps"] is not None:
        raise TruthError("This clip already has its final truth; its counts cannot change.")
    if row["finished"]:
        raise TruthError("This count is finished. Start a new count instead of changing it.")
    classes = {c["id"] for c in get_project(slug)["classes"]}
    clean = _clean_taps(taps, clip, classes)
    with db.connect(folder) as con:
        con.execute("UPDATE truth_counts SET taps = ?, finished = ? WHERE id = ?",
                    (json.dumps(clean), 1 if finished else 0, count_id))
    if finished:
        activity.log(folder, "truth.count",
                     f"انتهى عدّ المقطع #{clip['id']}: {len(clean)} عبور.",
                     f"Count of clip #{clip['id']} finished: {len(clean)} crossings.",
                     why_ar="كل مقطع يُعدّ مرتين، لأن العدّ البشري نفسه فيه خطأ؛ الفرق بين العدّتين يُحسم قبل الاستخدام.",
                     why_en="Every clip is counted twice, because human counting has errors too; differences are settled before use.",
                     outputs={"clip_id": clip["id"], "count_id": count_id}, numbers={"taps": len(clean)})
    return get_clip(slug, clip["id"])


def reconcile(slug: str, clip_id: int) -> dict[str, Any]:
    """Compare the last two finished counts tap by tap."""
    clip = get_clip(slug, clip_id)
    finished = [c for c in clip["counts"] if c["finished"]]
    if len(finished) < 2:
        return {"status": "needs_counts", "finished_counts": len(finished)}
    a, b = finished[-2], finished[-1]
    ta, tb = a["taps"], b["taps"]
    xy = all(t["x"] is not None for t in ta + tb)
    m = match([t["t"] for t in ta], [t["t"] for t in tb], window_s=AGREE_WINDOW_S,
              truth_xy=[(t["x"], t["y"]) for t in ta] if xy else None,
              event_xy=[(t["x"], t["y"]) for t in tb] if xy else None, window_px=60.0)
    pairs = [{"a": i, "b": j, "t": round((ta[i]["t"] + tb[j]["t"]) / 2, 3),
              "class_a": ta[i]["class_id"], "class_b": tb[j]["class_id"]} for i, j in m.matched]
    conflicts = [p for p in pairs if p["class_a"] != p["class_b"]]
    only_a = [{"index": i, **ta[i]} for i in m.misses]
    only_b = [{"index": j, **tb[j]} for j in m.extras]
    agreed = not conflicts and not only_a and not only_b
    return {
        "status": "agreed" if agreed else "disagree",
        "count_a": {"id": a["id"], "counter": a["counter"], "n": len(ta)},
        "count_b": {"id": b["id"], "counter": b["counter"], "n": len(tb)},
        "pairs": len(pairs),
        "class_conflicts": conflicts,
        "only_in_a": only_a,
        "only_in_b": only_b,
    }


def resolve(slug: str, clip_id: int, decisions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the final truth. Every disagreement needs a decision:
      decisions = {"keep_a": [indexes], "keep_b": [indexes], "class": {"<a index>": class_id}}
    Taps found by only one counter are dropped unless kept; class conflicts must be decided.
    """
    decisions = decisions or {}
    folder = project_dir(slug)
    clip = get_clip(slug, clip_id)
    if clip["final_taps"] is not None:
        raise TruthError("This clip already has its final truth.")
    rec = reconcile(slug, clip_id)
    if rec["status"] == "needs_counts":
        raise TruthError("Two finished counts are needed before the truth can be final.")
    finished = [c for c in clip["counts"] if c["finished"]]
    ta, tb = finished[-2]["taps"], finished[-1]["taps"]
    class_choice = {int(k): v for k, v in (decisions.get("class") or {}).items()}
    missing = [c["a"] for c in rec["class_conflicts"] if c["a"] not in class_choice]
    if missing:
        raise TruthError(f"{len(missing)} crossings have a different type in the two counts; choose the type for each.")
    final = []
    xy = all(t["x"] is not None for t in ta + tb)
    m = match([t["t"] for t in ta], [t["t"] for t in tb], window_s=AGREE_WINDOW_S,
              truth_xy=[(t["x"], t["y"]) for t in ta] if xy else None,
              event_xy=[(t["x"], t["y"]) for t in tb] if xy else None, window_px=60.0)
    for i, j in m.matched:
        a, b = ta[i], tb[j]
        cls = class_choice.get(i, a["class_id"]) if a["class_id"] != b["class_id"] else a["class_id"]
        both_xy = a["x"] is not None and b["x"] is not None
        final.append({"t": round((a["t"] + b["t"]) / 2, 3), "class_id": cls,
                      "x": round((a["x"] + b["x"]) / 2, 1) if both_xy else a["x"] if a["x"] is not None else b["x"],
                      "y": round((a["y"] + b["y"]) / 2, 1) if both_xy else a["y"] if a["y"] is not None else b["y"]})
    keep_a, keep_b = set(decisions.get("keep_a") or []), set(decisions.get("keep_b") or [])
    final += [ta[i] for i in m.misses if i in keep_a]
    final += [tb[j] for j in m.extras if j in keep_b]
    final.sort(key=lambda t: t["t"])
    dropped = len(m.misses) + len(m.extras) - len(keep_a & set(m.misses)) - len(keep_b & set(m.extras))
    with db.connect(folder) as con:
        con.execute("UPDATE truth_clips SET final_taps = ?, final_by = ? WHERE id = ?",
                    (json.dumps(final), current_user(), clip_id))
    activity.log(folder, "truth.final",
                 f"المسطرة النهائية للمقطع #{clip_id}: {len(final)} عبور "
                 f"(اتفقت العدّتان على {len(m.matched)}، حُذف {dropped}).",
                 f"Final truth for clip #{clip_id}: {len(final)} crossings "
                 f"(both counts agreed on {len(m.matched)}, {dropped} dropped).",
                 numbers={"final": len(final), "agreed": len(m.matched), "dropped": dropped,
                          "class_decisions": len(class_choice)},
                 outputs={"clip_id": clip_id})
    return get_clip(slug, clip_id)


def lock_clip(slug: str, clip_id: int) -> dict[str, Any]:
    clip = get_clip(slug, clip_id)
    if clip["final_taps"] is None:
        raise TruthError("Only a clip with final truth can be locked.")
    with db.connect(project_dir(slug)) as con:
        con.execute("UPDATE truth_clips SET locked = 1 WHERE id = ?", (clip_id,))
    activity.log(project_dir(slug), "truth.lock", f"قُفل مقطع المسطرة #{clip_id}؛ لن يتغير بعد الآن.",
                 f"Truth clip #{clip_id} locked; it will not change any more.")
    return get_clip(slug, clip_id)
