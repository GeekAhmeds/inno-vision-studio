"""Register media in a project (prompt §5.2, §5.4, §6 module 1).

- A video is linked in place (large footage stays where it is) or copied into the project.
- Every file is hashed, so the same file is never registered twice and a changed file is seen.
- Each camera angle has an id and a reference frame; media of another resolution is refused
  for that angle. (The full "camera moved" comparison arrives with `inno_count` in P1.)
- Folder per product: sub-folder name -> class; each picture gets a whole-image class label
  with `human:<user>` provenance, because the owner sorted the pictures himself.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from .. import db
from ..config import current_user
from ..explain import activity
from ..projects import add_class, project_dir

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".ts", ".m4v", ".webm", ".flv", ".dav"}


class IngestError(Exception):
    """A problem the owner can understand and fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_image(path: Path) -> np.ndarray | None:
    # imdecode on bytes, because cv2.imread fails on non-ASCII (Arabic) paths on Windows.
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _write_jpg(path: Path, image: np.ndarray) -> None:
    ok, buf = cv2.imencode(".jpg", image)
    if not ok:
        raise IngestError("Could not encode the reference frame.")
    path.write_bytes(buf.tobytes())


def _existing(folder: Path, sha: str) -> int | None:
    with db.connect(folder) as con:
        row = con.execute("SELECT id FROM media WHERE sha256 = ?", (sha,)).fetchone()
    return row["id"] if row else None


def _free_name(directory: Path, name: str) -> Path:
    target, n = directory / name, 2
    while target.exists():
        target, n = directory / f"{Path(name).stem}_{n}{Path(name).suffix}", n + 1
    return target


def probe_video(path: Path) -> tuple[dict[str, Any], np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise IngestError(f"Could not open the video (unsupported or damaged): {path.name}")
        ok, first = cap.read()
        if not ok or first is None:
            raise IngestError(f"The video has no readable frame: {path.name}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()
    info = {
        "width": int(first.shape[1]),
        "height": int(first.shape[0]),
        "fps": round(fps, 3),
        "frames": frames,
        "duration_s": round(frames / fps, 3) if fps > 0 else None,
    }
    return info, first


def _check_angle(folder: Path, angle_id: str, info: dict[str, Any], first: np.ndarray) -> bool:
    """Store the reference frame for a new angle; refuse a resolution that differs from it."""
    angle_dir = folder / "sources" / "angles" / angle_id
    meta_path = angle_dir / "angle.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta["width"], meta["height"]) != (info["width"], info["height"]):
            raise IngestError(
                f"This video is {info['width']}×{info['height']} but camera angle “{angle_id}” is "
                f"{meta['width']}×{meta['height']}. Give it a new angle id; angles are never mixed."
            )
        return False
    angle_dir.mkdir(parents=True, exist_ok=True)
    _write_jpg(angle_dir / "reference.jpg", first)
    meta_path.write_text(json.dumps({"angle_id": angle_id, "width": info["width"], "height": info["height"],
                                     "created_at": _now()}), encoding="utf-8")
    return True


def add_video(slug: str, path: str | Path, *, mode: str = "link", angle_id: str = "angle-1",
              source_id: str | None = None) -> dict[str, Any]:
    """Register one video. mode='link' leaves it in place; mode='copy' copies it into the project.
    A file already inside the project folder (an upload or a recording) is registered where it is."""
    folder = project_dir(slug)
    path = Path(path).resolve()
    if not path.is_file():
        raise IngestError(f"Video file not found: {path}")
    if mode not in ("link", "copy"):
        raise IngestError("mode must be 'link' or 'copy'.")
    started = time.monotonic()
    info, first = probe_video(path)
    sha = _sha256(path)
    existing = _existing(folder, sha)
    if existing is not None:
        raise IngestError(f"This exact video is already in the project (media #{existing}).")
    new_angle = _check_angle(folder, angle_id, info, first)

    rel_path, linked_path = None, None
    inside = folder.resolve() in path.parents
    if inside:
        rel_path = path.relative_to(folder.resolve()).as_posix()
    elif mode == "copy":
        target = _free_name(folder / "media" / "videos", path.name)
        shutil.copy2(path, target)
        rel_path = target.relative_to(folder).as_posix()
    else:
        linked_path = str(path)

    with db.connect(folder) as con:
        cur = con.execute(
            "INSERT INTO media (kind, rel_path, linked_path, sha256, bytes, width, height, fps, frames,"
            " duration_s, source_id, angle_id, origin_name, added_at) VALUES ('video',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rel_path, linked_path, sha, path.stat().st_size, info["width"], info["height"], info["fps"],
             info["frames"], info["duration_s"], source_id, angle_id, path.name, _now()),
        )
        media_id = cur.lastrowid
    minutes = (info["duration_s"] or 0) / 60
    activity.log(
        folder, "ingest.video",
        f"سُجّل الفيديو «{path.name}»: {info['width']}×{info['height']}، {info['fps']} صورة/ث، {minutes:.1f} دقيقة.",
        f"Video “{path.name}” registered: {info['width']}×{info['height']}, {info['fps']} fps, {minutes:.1f} min.",
        why_ar=("أول فيديو لهذه الزاوية: حُفظت منه صورة مرجعية لاكتشاف تحرّك الكاميرا لاحقًا."
                if new_angle else "الملف له بصمة (hash) فلا يُسجَّل مرتين ويُكتشف أي تغيير فيه."),
        why_en=("First video for this angle: a reference frame was stored to detect a moved camera later."
                if new_angle else "The file is hashed, so it is never registered twice and a change is detected."),
        inputs={"mode": "in project" if inside else mode, "angle_id": angle_id, "source_id": source_id},
        outputs={"media_id": media_id},
        numbers={**info, "bytes": path.stat().st_size},
        duration_s=round(time.monotonic() - started, 2),
    )
    return {"id": media_id, "kind": "video", "sha256": sha, "rel_path": rel_path, "linked_path": linked_path,
            "angle_id": angle_id, **info}


def _add_image(folder: Path, path: Path, con) -> tuple[int | None, str]:
    """Copy one picture into the project. Returns (media id or None, 'added'|'duplicate'|'unreadable')."""
    image = _read_image(path)
    if image is None:
        return None, "unreadable"
    sha = _sha256(path)
    row = con.execute("SELECT id FROM media WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        return row["id"], "duplicate"
    # Stored under the hash: no name clashes, no non-ASCII file names inside the project.
    target = folder / "media" / "images" / f"{sha[:16]}{path.suffix.lower()}"
    inside = folder.resolve() in path.resolve().parents
    if inside:
        if path.resolve() != target.resolve():
            shutil.move(str(path), target)
    else:
        shutil.copy2(path, target)
    cur = con.execute(
        "INSERT INTO media (kind, rel_path, sha256, bytes, width, height, origin_name, added_at)"
        " VALUES ('image',?,?,?,?,?,?,?)",
        (target.relative_to(folder).as_posix(), sha, target.stat().st_size, int(image.shape[1]),
         int(image.shape[0]), path.name, _now()),
    )
    return cur.lastrowid, "added"


def _image_files(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXT))
        elif p.is_file() and p.suffix.lower() in IMAGE_EXT:
            out.append(p)
    return out


def add_images(slug: str, paths: Iterable[str | Path]) -> dict[str, Any]:
    """Register loose pictures (files or folders). They carry no label yet."""
    folder = project_dir(slug)
    started = time.monotonic()
    counts = {"added": 0, "duplicate": 0, "unreadable": 0}
    with db.connect(folder) as con:
        for path in _image_files(Path(p) for p in paths):
            _, status = _add_image(folder, path, con)
            counts[status] += 1
    activity.log(
        folder, "ingest.images",
        f"أُضيفت {counts['added']} صورة (مكررة: {counts['duplicate']}، غير مقروءة: {counts['unreadable']}).",
        f"{counts['added']} pictures added (duplicates: {counts['duplicate']}, unreadable: {counts['unreadable']}).",
        why_ar="هذه الصور بلا تسمية بعد؛ ستُعرض عليك لاحقًا لتسميتها.",
        why_en="These pictures have no label yet; they will be shown to you for labelling later.",
        numbers=counts, duration_s=round(time.monotonic() - started, 2),
    )
    return counts


def add_product_folders(slug: str, root: str | Path) -> dict[str, Any]:
    """Folder per product: every sub-folder of `root` is a product; its pictures get that class."""
    folder = project_dir(slug)
    root = Path(root)
    if not root.is_dir():
        raise IngestError(f"Folder not found: {root}")
    products = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith((".", "__")))
    if not products:
        raise IngestError("This folder has no sub-folders. Make one sub-folder per product and put its pictures inside.")
    started = time.monotonic()
    provenance = f"human:{current_user()}"
    per_class: dict[str, dict[str, Any]] = {}
    totals = {"added": 0, "duplicate": 0, "unreadable": 0}
    for product in products:
        files = _image_files([product])
        if not files:
            continue
        cls = add_class(slug, product.name)
        labelled = 0
        with db.connect(folder) as con:
            for path in files:
                media_id, status = _add_image(folder, path, con)
                totals[status] += 1
                if media_id is None:
                    continue
                already = con.execute(
                    "SELECT 1 FROM labels WHERE media_id = ? AND class_id = ? AND bbox IS NULL",
                    (media_id, cls["id"])).fetchone()
                if not already:
                    con.execute(
                        "INSERT INTO labels (media_id, class_id, bbox, provenance, created_at) VALUES (?,?,NULL,?,?)",
                        (media_id, cls["id"], provenance, _now()))
                    labelled += 1
        per_class[cls["id"]] = {"display_name": cls["display_name"], "pictures": labelled}
    if not per_class:
        raise IngestError("No pictures were found inside the product folders.")
    summary_ar = "، ".join(f"{v['display_name']}: {v['pictures']}" for v in per_class.values())
    summary_en = ", ".join(f"{v['display_name']}: {v['pictures']}" for v in per_class.values())
    activity.log(
        folder, "ingest.product_folders",
        f"أُضيفت صور {len(per_class)} منتج ({summary_ar}).",
        f"Pictures of {len(per_class)} products added ({summary_en}).",
        why_ar=("اسم كل فولدر أصبح نوعًا، وتسميتك تُعتبر مراجعة بشرية لنوع الصورة. "
                "تنبيه: هذه الصور تعرّف الأنواع لكنها لا تغني عن تصوير من الكاميرا الحقيقية، "
                "ولتدريب كاشف (detector) ما زالت الصناديق تحتاج تأكيدك."),
        why_en=("Each folder name became a class, and your sorting counts as human review of the picture's class. "
                "Note: these pictures define the classes but do not replace footage from the real camera, "
                "and a detector still needs boxes that you confirm."),
        numbers={**totals, "classes": len(per_class)},
        outputs={"classes": per_class},
        duration_s=round(time.monotonic() - started, 2),
    )
    return {"classes": per_class, **totals}


def list_media(slug: str, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    folder = project_dir(slug)
    query = ("SELECT m.*, (SELECT GROUP_CONCAT(l.class_id) FROM labels l WHERE l.media_id = m.id) AS classes"
             " FROM media m")
    args: tuple = ()
    if kind:
        query, args = query + " WHERE m.kind = ?", (kind,)
    query += " ORDER BY m.id DESC LIMIT ?"
    with db.connect(folder) as con:
        rows = [dict(r) for r in con.execute(query, (*args, limit))]
    for row in rows:
        row["missing"] = bool(row["linked_path"]) and not Path(row["linked_path"]).exists()
    return rows
