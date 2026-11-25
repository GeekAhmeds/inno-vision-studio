"""Pick frames for labelling by diversity, not uniformly (prompt §6 module 2).

1. Scan each video every `every_s` seconds: a tiny grey thumbnail, brightness, a difference hash,
   and motion (how much the picture changes over the next ~0.2 s).
2. Never take a frame from a truth clip, or within 10 minutes of one in the same video (rule 3).
3. Keep a share of the quietest frames as likely negatives (empty belt, stopped line) — the owner
   confirms them as empty in review; low motion alone does not prove a frame is empty.
4. Spread the rest over time (equal share per time slice, so day and night both appear) and, inside
   each slice, take the frames that look most unlike the ones already taken.
5. Drop near-duplicates (difference hash within a few bits), also against frames sampled earlier.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from inno_count.engine import frame_at

from . import db
from .explain import activity
from .harness.run import media_path
from .projects import project_dir

TRUTH_BUFFER_S = 600.0


class SampleError(Exception):
    """A problem the owner can understand and fix."""


@dataclass
class Candidate:
    media_id: int
    t: float
    thumb: np.ndarray
    brightness: float
    motion: float
    dhash: int


def dhash(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _gray(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), size, interpolation=cv2.INTER_AREA).astype(np.float32)


def scan_video(path: Path, media_id: int, every_s: float, skip: list[tuple[float, float]],
               progress: Callable[[float], None] | None = None) -> list[Candidate]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SampleError(f"Could not open the video: {path.name}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frames / fps if frames else 0.0
    ahead = max(1, round(0.2 * fps))
    out: list[Candidate] = []
    sequential = every_s * fps <= 3 * ahead   # close samples: reading on is cheaper than seeking
    try:
        t = 0.0
        while duration == 0 or t < duration:
            if any(a <= t <= b for a, b in skip):
                t += every_s
                continue
            if not sequential or not out:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            later = None
            for _ in range(ahead):
                ok2, nxt = cap.read()
                if not ok2:
                    break
                later = nxt
            thumb = _gray(frame, (32, 18))
            motion = float(np.abs(_gray(later, (32, 18)) - thumb).mean()) if later is not None else 0.0
            out.append(Candidate(media_id, round(actual if actual > 0 else t, 3), thumb / 255.0,
                                 float(thumb.mean()), motion, dhash(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))))
            if sequential:
                skip_frames = max(0, round(every_s * fps) - ahead - 1)
                for _ in range(skip_frames):
                    if not cap.grab():
                        break
            t += every_s
            if progress and duration:
                progress(min(t / duration, 1.0))
    finally:
        cap.release()
    return out


def select(cands: list[Candidate], n: int, negative_share: float = 0.15, dedupe_bits: int = 5,
           known_hashes: list[int] | None = None) -> list[tuple[Candidate, str]]:
    """Returns (candidate, reason) with reason 'low_motion' or 'diverse'."""
    known = list(known_hashes or [])
    pool = []
    for c in sorted(cands, key=lambda c: (c.media_id, c.t)):
        if all(hamming(c.dhash, h) > dedupe_bits for h in known):
            pool.append(c)
            known.append(c.dhash)
    if not pool or n <= 0:
        return []
    chosen: list[tuple[Candidate, str]] = []

    # Likely negatives: the quietest frames, spread over time.
    n_neg = min(int(round(n * negative_share)), len(pool))
    if n_neg:
        motions = np.array([c.motion for c in pool])
        quiet = [c for c in pool if c.motion <= np.quantile(motions, 0.2)]
        idx = np.linspace(0, len(quiet) - 1, min(n_neg, len(quiet))).round().astype(int) if quiet else []
        for i in sorted(set(int(i) for i in idx)):
            chosen.append((quiet[i], "low_motion"))
    taken = {id(c) for c, _ in chosen}
    rest = [c for c in pool if id(c) not in taken]

    # Diverse frames: equal share per time slice, farthest-point inside each slice.
    n_div = min(n - len(chosen), len(rest))
    if n_div > 0:
        slices = np.array_split(np.arange(len(rest)), min(n_div, max(1, min(24, len(rest)))))
        per = [n_div // len(slices) + (1 if i < n_div % len(slices) else 0) for i in range(len(slices))]
        feats = np.stack([np.concatenate([c.thumb.flatten(), [c.brightness / 64.0, c.motion / 16.0]]) for c in rest])
        picked_feats = [np.concatenate([c.thumb.flatten(), [c.brightness / 64.0, c.motion / 16.0]]) for c, _ in chosen]
        for sl, k in zip(slices, per):
            members = list(sl)
            for _ in range(min(k, len(members))):
                if picked_feats:
                    ref = np.stack(picked_feats)
                    dist = np.min(np.linalg.norm(feats[members][:, None, :] - ref[None, :, :], axis=2), axis=1)
                    j = members[int(np.argmax(dist))]
                else:
                    j = max(members, key=lambda m: rest[m].motion)
                chosen.append((rest[j], "diverse"))
                picked_feats.append(feats[j])
                members.remove(j)
    return chosen


def truth_ranges(slug: str) -> dict[int, list[tuple[float, float]]]:
    with db.connect(project_dir(slug)) as con:
        rows = con.execute("SELECT media_id, start_s, end_s FROM truth_clips").fetchall()
    out: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        out.setdefault(r["media_id"], []).append((max(0.0, r["start_s"] - TRUTH_BUFFER_S), r["end_s"] + TRUTH_BUFFER_S))
    return out


def run_sample(slug: str, media_ids: list[int] | None = None, n: int = 200, every_s: float = 5.0,
               negative_share: float = 0.15, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    folder = project_dir(slug)
    if n < 1 or n > 5000:
        raise SampleError("Choose between 1 and 5000 frames.")
    if every_s < 0.2:
        raise SampleError("Scan at most every 0.2 s.")
    started = time.monotonic()
    with db.connect(folder) as con:
        videos = [dict(r) for r in con.execute("SELECT * FROM media WHERE kind = 'video' ORDER BY id")]
        known = [int(r["phash"], 16) for r in con.execute("SELECT phash FROM media WHERE phash IS NOT NULL")]
    if media_ids:
        videos = [v for v in videos if v["id"] in set(media_ids)]
    if not videos:
        raise SampleError("Add a video first (Data tab).")
    skip = truth_ranges(slug)
    cands: list[Candidate] = []
    for i, v in enumerate(videos):
        path = media_path(slug, v["id"])

        def step(f: float, i=i, v=v) -> None:
            if progress:
                progress(0.85 * (i + f) / len(videos), f"فحص {v['origin_name']} ({i + 1}/{len(videos)})")

        cands += scan_video(path, v["id"], every_s, skip.get(v["id"], []), step)
    chosen = select(cands, n, negative_share, known_hashes=known)

    out_dir = folder / "media" / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    added = {"diverse": 0, "low_motion": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect(folder) as con:
        for k, (c, reason) in enumerate(chosen):
            frame = frame_at(media_path(slug, c.media_id), c.t)
            if frame is None:
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            data = buf.tobytes()
            sha = hashlib.sha256(data).hexdigest()
            if con.execute("SELECT 1 FROM media WHERE sha256 = ?", (sha,)).fetchone():
                continue
            target = out_dir / f"v{c.media_id}_{c.t:010.3f}.jpg"
            target.write_bytes(data)
            con.execute(
                "INSERT INTO media (kind, rel_path, sha256, bytes, width, height, origin_name, added_at,"
                " source_media_id, source_t, phash, motion, sample_reason, angle_id)"
                " VALUES ('image',?,?,?,?,?,?,?,?,?,?,?,?,(SELECT angle_id FROM media WHERE id = ?))",
                (target.relative_to(folder).as_posix(), sha, len(data), frame.shape[1], frame.shape[0], target.name, now,
                 c.media_id, c.t, f"{c.dhash:016x}", round(c.motion, 3), reason, c.media_id))
            added[reason] += 1
            if progress and k % 10 == 0:
                progress(0.85 + 0.15 * k / max(len(chosen), 1), "حفظ الصور المختارة")
    excluded = sum(len(r) for r in skip.values())
    entry = activity.log(
        folder, "sample.frames",
        f"اختيرت {sum(added.values())} صورة للتسمية من {len(cands)} لقطة مفحوصة "
        f"({added['diverse']} متنوعة، {added['low_motion']} هادئة يُحتمل أنها فاضية).",
        f"{sum(added.values())} frames chosen for labelling out of {len(cands)} scanned "
        f"({added['diverse']} diverse, {added['low_motion']} quiet, likely empty).",
        why_ar=("الاختيار بالتنوع لا بالتساوي: نفس الشكل المتكرر لا يعلّم الموديل شيئًا جديدًا. "
                "الصور الهادئة تُراجَع كأمثلة سلبية (سير فاضي)، وهي إلزامية. "
                f"لم تُؤخذ أي صورة من مقاطع المسطرة ولا من {int(TRUTH_BUFFER_S / 60)} دقائق حولها."),
        why_en=("Chosen for diversity, not evenly: the same view repeated teaches nothing new. Quiet frames are "
                "reviewed as negatives (empty belt), which are mandatory. Nothing was taken from truth clips "
                f"or within {int(TRUTH_BUFFER_S / 60)} minutes of them."),
        inputs={"videos": [v["id"] for v in videos], "n": n, "every_s": every_s, "negative_share": negative_share},
        numbers={"scanned": len(cands), **added, "truth_ranges_excluded": excluded},
        duration_s=round(time.monotonic() - started, 1))
    return {"scanned": len(cands), **added, "log": entry["ar"]}
