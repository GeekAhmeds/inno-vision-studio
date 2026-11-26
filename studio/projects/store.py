"""Project store (prompt §5.1).

A project is a folder under `<workspace>/projects/<slug>/`. The slug is ASCII and never changes;
the display name can be anything (Arabic included) and can be renamed. Paths stored inside a
project are relative and posix-style, so an exported project opens on Linux and Windows alike.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .. import db
from ..config import projects_dir
from ..explain import activity

PROJECT_FILE = "project.yaml"
MANIFEST_FILE = "export_manifest.json"
SCHEMA_VERSION = 1

SUBDIRS = [
    "sources",
    "media/videos",
    "media/images",
    "datasets",
    "truth",
    "models",
    "reports",
]

TEMPLATES: dict[str, dict[str, str]] = {
    "count_line": {
        "ar": "عدّ على خط",
        "en": "Count objects crossing a line",
    },
    "count_per_type": {
        "ar": "عدّ لكل نوع",
        "en": "Count per type",
    },
    "classify": {
        "ar": "تصنيف صور",
        "en": "Classify pictures",
    },
}

CLASS_COLOURS = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d"]


class ProjectError(Exception):
    """A problem the owner can understand and fix. The message is shown as is."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48]


def _unique_slug(base: str) -> str:
    base = base or f"project-{uuid.uuid4().hex[:6]}"
    slug, n = base, 2
    while (projects_dir() / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def project_dir(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
        raise ProjectError(f"Invalid project id: {slug!r}")
    path = projects_dir() / slug
    if not (path / PROJECT_FILE).exists():
        raise ProjectError(f"Project not found: {slug}")
    return path


def get_project(slug: str) -> dict[str, Any]:
    return yaml.safe_load((project_dir(slug) / PROJECT_FILE).read_text(encoding="utf-8"))


def save_project(meta: dict[str, Any]) -> None:
    path = projects_dir() / meta["slug"] / PROJECT_FILE
    path.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")


def list_projects(include_archived: bool = True) -> list[dict[str, Any]]:
    out = []
    for folder in sorted(projects_dir().iterdir()):
        if not (folder / PROJECT_FILE).exists():
            continue
        meta = yaml.safe_load((folder / PROJECT_FILE).read_text(encoding="utf-8"))
        if meta.get("archived") and not include_archived:
            continue
        meta["stats"] = stats(folder)
        out.append(meta)
    return out


def stats(folder: Path) -> dict[str, Any]:
    with db.connect(folder) as con:
        videos = con.execute("SELECT COUNT(*), COALESCE(SUM(duration_s), 0) FROM media WHERE kind='video'").fetchone()
        images = con.execute("SELECT COUNT(*) FROM media WHERE kind='image'").fetchone()[0]
        human = con.execute("SELECT COUNT(*) FROM labels WHERE provenance LIKE 'human:%'").fetchone()[0]
        other = con.execute("SELECT COUNT(*) FROM labels WHERE provenance NOT LIKE 'human:%'").fetchone()[0]
    models = [p.name for p in (folder / "models").iterdir() if p.is_dir()]
    entries = activity.read(folder, limit=1)
    return {
        "videos": videos[0],
        "video_hours": round(videos[1] / 3600, 2),
        "images": images,
        "labels_human": human,
        "labels_unreviewed": other,
        "models": len(models),
        "last_activity": entries[0]["ts"] if entries else None,
    }


def create_project(name: str, product: str = "", template: str = "count_line", slug: str | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ProjectError("The project needs a name.")
    if template not in TEMPLATES:
        raise ProjectError(f"Unknown template: {template}")
    slug = _unique_slug(slugify(slug or name))
    folder = projects_dir() / slug
    for sub in SUBDIRS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "name": name,
        "product": product.strip(),
        "task_type": template,
        "created_at": _now(),
        "archived": False,
        "classes": [],
    }
    save_project(meta)
    db.init(folder)
    activity.log(
        folder,
        "project.create",
        f"تم إنشاء المشروع «{name}» ({TEMPLATES[template]['ar']}).",
        f"Project “{name}” created ({TEMPLATES[template]['en']}).",
        why_ar="كل مشروع له فولدر مستقل يحفظ مصادره وبياناته وموديلاته، فلا يختلط منتج بآخر.",
        why_en="Each project keeps its own sources, data and models, so products never mix.",
        outputs={"slug": slug, "folder": str(folder)},
    )
    return meta


def rename_project(slug: str, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ProjectError("The project needs a name.")
    meta = get_project(slug)
    old, meta["name"] = meta["name"], name
    save_project(meta)
    activity.log(project_dir(slug), "project.rename", f"تغيّر اسم المشروع من «{old}» إلى «{name}».",
                 f"Project renamed from “{old}” to “{name}”.")
    return meta


def archive_project(slug: str, archived: bool = True) -> dict[str, Any]:
    meta = get_project(slug)
    meta["archived"] = archived
    save_project(meta)
    word_ar, word_en = ("أُرشف", "archived") if archived else ("أُعيد من الأرشيف", "restored")
    activity.log(project_dir(slug), "project.archive", f"المشروع {word_ar}.", f"Project {word_en}.")
    return meta


def duplicate_project(slug: str, new_name: str) -> dict[str, Any]:
    src = project_dir(slug)
    meta = get_project(slug)
    new_slug = _unique_slug(slugify(new_name) or f"{slug}-copy")
    dst = projects_dir() / new_slug
    shutil.copytree(src, dst)
    meta.update(slug=new_slug, name=new_name.strip() or f"{meta['name']} (copy)", created_at=_now(), archived=False)
    save_project(meta)
    activity.log(dst, "project.duplicate", f"نسخة من المشروع «{slug}».", f"Duplicated from project “{slug}”.",
                 inputs={"from": slug})
    return meta


def delete_project(slug: str, confirm: str) -> None:
    """Deleting is permanent, so the caller must repeat the slug."""
    folder = project_dir(slug)
    if confirm != slug:
        raise ProjectError("To delete, type the project id exactly.")
    db.forget(folder)
    shutil.rmtree(folder)


def add_class(slug: str, display_name: str, class_id: str | None = None) -> dict[str, Any]:
    """Classes have a stable ASCII id and a display name in any language (prompt §5.5)."""
    meta = get_project(slug)
    display_name = display_name.strip()
    if not display_name:
        raise ProjectError("The class needs a name.")
    for existing in meta["classes"]:
        if existing["display_name"] == display_name:
            return existing
    taken = {c["id"] for c in meta["classes"]}
    base = slugify(class_id or display_name).replace("-", "_")
    # A name with no ASCII letters (Arabic) gets a numbered id; the name stays as display name.
    cid, n = (base, 2) if base else ("class_1", 2)
    base = base or "class"
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    entry = {"id": cid, "display_name": display_name, "colour": CLASS_COLOURS[len(taken) % len(CLASS_COLOURS)]}
    meta["classes"].append(entry)
    save_project(meta)
    return entry


# ---------------------------------------------------------------- export / import


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_project(slug: str, dest: Path) -> Path:
    """Write the whole project as one zip with a hash manifest.

    Secrets are never inside a project folder (they live in the OS keyring), so nothing needs
    stripping. Media that was linked in place is not copied; it is listed so the importer knows
    what to reconnect.
    """
    folder = project_dir(slug)
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / f"{slug}.ivs.zip"
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.name != MANIFEST_FILE)
    manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "slug": slug, "exported_at": _now(), "files": {}}
    with db.connect(folder) as con:
        manifest["linked_media"] = [
            {"path": r["linked_path"], "sha256": r["sha256"]}
            for r in con.execute("SELECT linked_path, sha256 FROM media WHERE linked_path IS NOT NULL")
        ]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in files:
            rel = path.relative_to(folder).as_posix()
            manifest["files"][rel] = _sha256(path)
            zf.write(path, rel)
        zf.writestr(MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=1))
    activity.log(folder, "project.export", f"صُدّر المشروع إلى ملف واحد ({len(files)} ملف).",
                 f"Project exported to one archive ({len(files)} files).",
                 why_ar="للنسخ الاحتياطي أو للنقل إلى جهاز آخر (لينكس أو ويندوز).",
                 why_en="For backup, or to move to another machine (Linux or Windows).",
                 outputs={"archive": str(dest)}, numbers={"files": len(files), "linked_media": len(manifest["linked_media"])})
    return dest


def import_project(archive: Path) -> dict[str, Any]:
    """Unpack an exported project, verify every file hash, and register it under a free slug."""
    archive = Path(archive)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        if MANIFEST_FILE not in names or PROJECT_FILE not in names:
            raise ProjectError("This file is not a studio project export.")
        manifest = json.loads(zf.read(MANIFEST_FILE))
        slug = _unique_slug(slugify(manifest.get("slug", "")))
        folder = projects_dir() / slug
        root = folder.resolve()
        try:
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ProjectError(f"Unsafe path in archive: {name}")
                target = (folder / Path(*pure.parts)).resolve()
                if root not in target.parents and target != root:
                    raise ProjectError(f"Unsafe path in archive: {name}")
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
            for rel, expected in manifest["files"].items():
                if _sha256(folder / Path(*PurePosixPath(rel).parts)) != expected:
                    raise ProjectError(f"File damaged in archive: {rel}")
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
    for sub in SUBDIRS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    (folder / MANIFEST_FILE).unlink(missing_ok=True)
    db.forget(folder)
    meta = yaml.safe_load((folder / PROJECT_FILE).read_text(encoding="utf-8"))
    meta["slug"] = slug
    save_project(meta)
    missing = [m["path"] for m in manifest.get("linked_media", []) if not Path(m["path"]).exists()]
    activity.log(folder, "project.import", f"استُورد المشروع وتم التحقق من {len(manifest['files'])} ملف.",
                 f"Project imported; {len(manifest['files'])} files verified by hash.",
                 inputs={"archive": archive.name},
                 numbers={"files": len(manifest["files"]), "linked_media_missing": len(missing)},
                 outputs={"linked_media_missing": missing})
    return meta


# ---------------------------------------------------------------- counting setup

COUNTING_DEFAULTS: dict[str, Any] = {
    "line": None,               # [x1, y1, x2, y2] in frame pixels; objects count moving to its "after" side
    "direction": "positive",    # positive | negative | both
    "angle_id": "angle-1",
    "hysteresis_px": 6.0,
    "roi": None,                # [x1, y1, x2, y2] or None for the whole frame (older projects)
    "area": None,               # [[x, y], ...] polygon around the belt; wins over roi
    "entry_line": None,         # optional second line (e.g. the oven mouth): entries - counts = taken between
    "edge_margin_px": 3.0,
    "k_crops": 5,
    "classify_timeout_s": 2.0,
    "calibration": None,        # {"image_points": [[x,y]x4], "width_cm": w, "length_cm": l}
}


def get_counting(slug: str) -> dict[str, Any]:
    meta = get_project(slug)
    return {**COUNTING_DEFAULTS, **(meta.get("counting") or {})}


def set_counting(slug: str, changes: dict[str, Any]) -> dict[str, Any]:
    meta = get_project(slug)
    cfg = {**COUNTING_DEFAULTS, **(meta.get("counting") or {})}
    for key, value in changes.items():
        if key not in COUNTING_DEFAULTS:
            raise ProjectError(f"Unknown counting setting: {key}")
        cfg[key] = value
    line = cfg["line"]
    if line is not None:
        if len(line) != 4 or (line[0], line[1]) == (line[2], line[3]):
            raise ProjectError("The counting line needs two different points.")
        cfg["line"] = [round(float(v), 1) for v in line]
    entry = cfg["entry_line"]
    if entry is not None:
        if len(entry) != 4 or (entry[0], entry[1]) == (entry[2], entry[3]):
            raise ProjectError("The entry line needs two different points.")
        entry = [round(float(v), 1) for v in entry]
        if cfg["line"] is not None:
            # Same "after" side as the counting line, however the two points were clicked: the
            # normals (dy, -dx) of both lines must point the same way.
            lx, ly = cfg["line"][2] - cfg["line"][0], cfg["line"][3] - cfg["line"][1]
            ex, ey = entry[2] - entry[0], entry[3] - entry[1]
            if ly * ey + lx * ex < 0:
                entry = [entry[2], entry[3], entry[0], entry[1]]
        cfg["entry_line"] = entry
    if cfg["direction"] not in ("positive", "negative", "both"):
        raise ProjectError("Direction must be positive, negative or both.")
    if cfg["roi"] is not None and len(cfg["roi"]) != 4:
        raise ProjectError("The ROI needs four numbers: x1, y1, x2, y2.")
    if cfg["area"] is not None:
        try:
            from inno_count.geometry import Area
            cfg["area"] = [[round(float(x), 1), round(float(y), 1)] for x, y in cfg["area"]]
            Area(cfg["area"])
        except (TypeError, ValueError) as exc:
            raise ProjectError("The counting area needs at least 3 points that enclose some space.") from exc
    cal = cfg["calibration"]
    if cal is not None:
        if len(cal.get("image_points", [])) != 4 or float(cal.get("width_cm", 0)) <= 0 or float(cal.get("length_cm", 0)) <= 0:
            raise ProjectError("Calibration needs four points and two positive real distances in cm.")
    for key in ("hysteresis_px", "edge_margin_px", "classify_timeout_s"):
        cfg[key] = float(cfg[key])
        if cfg[key] < 0:
            raise ProjectError(f"{key} cannot be negative.")
    cfg["k_crops"] = int(cfg["k_crops"])
    if cfg["k_crops"] < 1:
        raise ProjectError("k_crops must be at least 1.")
    meta["counting"] = cfg
    save_project(meta)
    activity.log(project_dir(slug), "counting.setup", "حُدّثت إعدادات العدّ (خط العدّ والاتجاه).",
                 "Counting setup updated (line and direction).",
                 why_ar="خط العدّ هو المكان الذي يُحسب عنده الرغيف مرة واحدة عند عبوره.",
                 why_en="The counting line is where each object is counted once, as it crosses.",
                 outputs={k: cfg[k] for k in ("line", "entry_line", "direction", "angle_id", "roi", "area")})
    return cfg
