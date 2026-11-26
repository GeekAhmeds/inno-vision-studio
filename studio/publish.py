"""Publish, shadow, activate, rollback (P5, prompt §6 modules 10 and 12).

The order is fixed: a model that **passed the gate** is published *inactive* (a folder with the ONNX
and a full sidecar), then runs in **shadow** beside the current model for at least one production
day, and only then a person **activates** it. The previous model is kept: **rollback** is one step.

The published folder is what the agent loads. Its sidecar carries everything the agent needs to
count the same way the studio measured: the class map, the counting line, entry line and area,
the size calibration, the counting-core version, the dataset version and the gate report's hash.

Uploading to the Inno Source ERP (`/en/vision/api/v1/counter-app/model/upload/`) is not wired yet:
the field names must come from the agent code (HARDWARE-AGENT/training/productline.py). Until then
the folder is copied to the agent by hand or by the agent session.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import inno_count
from inno_count.detector import file_sha256, load_sidecar

from . import registry
from .config import current_user
from .explain import activity
from .gate import load_report
from .projects import get_counting, get_project, project_dir, save_project

SIDECAR_KEEP = ("names", "imgsz", "conf", "iou", "roi_crop", "format", "licence")


class PublishError(Exception):
    """A problem the owner can understand and fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_deployment(slug: str) -> dict[str, Any]:
    dep = get_project(slug).get("deployment") or {}
    return {"current": dep.get("current"), "pending": dep.get("pending"), "history": dep.get("history") or []}


def _save_deployment(slug: str, dep: dict[str, Any]) -> None:
    meta = get_project(slug)
    meta["deployment"] = dep
    save_project(meta)


def _reference_sha(slug: str, angle_id: str) -> str | None:
    ref = project_dir(slug) / "sources" / "angles" / angle_id / "reference.jpg"
    return file_sha256(ref) if ref.is_file() else None


def publish(slug: str, gate_report: str) -> dict[str, Any]:
    report = load_report(slug, gate_report)
    if report["verdict"] != "pass":
        raise PublishError("Only a model that passed the gate can be published. This report says: "
                           + "; ".join(report["reasons"] or [report["verdict"]]))
    entry = registry.require_shippable(report["candidate"]["id"])
    path = Path(entry["path"])
    if not path.is_file() or file_sha256(path) != report["candidate"]["sha256"]:
        raise PublishError("The model file changed (or moved) since the gate ran. Run the gate again.")
    dep = get_deployment(slug)
    cur_sha = (dep["current"] or {}).get("sha256")
    gated_against = (report["current"] or {}).get("sha256")
    if cur_sha != gated_against:
        raise PublishError("The model in use changed since this gate ran, so the comparison is out of date. "
                           "Run the gate again against the current model.")
    if dep["pending"]:
        raise PublishError(f"“{dep['pending']['name']}” is already waiting for activation. Activate it or drop it first.")
    if cur_sha == entry["sha256"]:
        raise PublishError("This model is already the one in use.")

    meta = get_project(slug)
    counting = get_counting(slug)
    side = load_sidecar(path)
    sha = entry["sha256"]
    folder = project_dir(slug) / "published" / sha[:12]
    tmp = folder.with_name(folder.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    shutil.copy2(path, tmp / "model.onnx")
    sidecar = {k: side[k] for k in SIDECAR_KEEP if k in side}
    sidecar.update({
        "model_sha256": sha,
        "licence": entry["licence"],
        "class_map": {c["id"]: c["display_name"] for c in meta["classes"]},
        "counting": {k: counting.get(k) for k in ("line", "entry_line", "area", "roi", "direction", "hysteresis_px",
                                                  "edge_margin_px", "angle_id", "k_crops", "classify_timeout_s")},
        "size_calibration": counting.get("calibration"),
        "reference_frame_sha": _reference_sha(slug, counting["angle_id"]),
        "counting_core_version": inno_count.__version__,
        "dataset_version": side.get("trained_on"),
        "gate_report": report["name"],
        "gate_report_sha": report["report_sha"],
        "gate_fps": report["settings"]["fps"],
        "project": slug,
        "published_at": _now(),
        "published_by": current_user(),
    })
    (tmp / "model.json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy2(project_dir(slug) / "reports" / report["name"] / "report.html", tmp / "gate_report.html")
    if folder.exists():
        shutil.rmtree(folder)
    tmp.replace(folder)
    pending = {"model_id": entry["id"], "name": entry["name"], "sha256": sha,
               "folder": folder.relative_to(project_dir(slug)).as_posix(), "gate_report": report["name"],
               "gate_report_sha": report["report_sha"], "published_at": sidecar["published_at"],
               "published_by": sidecar["published_by"], "shadow": []}
    dep["pending"] = pending
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "publish.inactive",
                 f"اتنشر «{entry['name']}» غير مفعّل في {pending['folder']}. الخطوة الجاية: تشغيل ظلّ يوم إنتاج كامل.",
                 f"Published {entry['name']} inactive.",
                 why_ar="الموديل الجديد بيشتغل جنب الحالي من غير ما يأثر على المخزون، لحد ما نتأكد منه في يوم حقيقي.",
                 why_en="It runs beside the current model without touching stock until a real day confirms it.",
                 outputs={"folder": pending["folder"], "gate_report_sha": report["report_sha"][:12]})
    return pending


def drop_pending(slug: str) -> None:
    dep = get_deployment(slug)
    if not dep["pending"]:
        raise PublishError("Nothing is waiting for activation.")
    name = dep["pending"]["name"]
    dep["pending"] = None
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "publish.dropped", f"اتلغى نشر «{name}» قبل التفعيل.", f"Dropped {name}.")


def add_shadow(slug: str, day: str, current_count: int, candidate_count: int, physical_count: int | None = None,
               note: str = "") -> dict[str, Any]:
    dep = get_deployment(slug)
    if not dep["pending"]:
        raise PublishError("Publish a model (inactive) before recording a shadow day.")
    if min(current_count, candidate_count) < 0 or (physical_count is not None and physical_count < 0):
        raise PublishError("Counts cannot be negative.")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise PublishError("The day is written as YYYY-MM-DD.") from exc
    rec = {"day": day, "current_count": int(current_count), "candidate_count": int(candidate_count),
           "physical_count": None if physical_count is None else int(physical_count), "note": note.strip(),
           "by": current_user(), "at": _now()}
    if physical_count:
        rec["current_error_pct"] = round(100 * (current_count - physical_count) / physical_count, 2)
        rec["candidate_error_pct"] = round(100 * (candidate_count - physical_count) / physical_count, 2)
    dep["pending"]["shadow"].append(rec)
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "shadow.day",
                 f"يوم ظلّ {day}: الحالي {current_count}، الجديد {candidate_count}"
                 + (f"، الفعلي {physical_count}" if physical_count is not None else "") + ".",
                 f"Shadow day {day} recorded.", outputs=rec)
    return rec


def activate(slug: str, skip_shadow_reason: str = "") -> dict[str, Any]:
    dep = get_deployment(slug)
    p = dep["pending"]
    if not p:
        raise PublishError("Nothing is waiting for activation.")
    if not p["shadow"] and not skip_shadow_reason.strip():
        raise PublishError("Run the new model in shadow for at least one production day and record it, "
                           "or write why you skip it.")
    now = _now()
    if dep["current"]:
        dep["history"].append({**dep["current"], "replaced_at": now})
    dep["current"] = {**p, "activated_at": now, "activated_by": current_user(),
                      "skip_shadow_reason": skip_shadow_reason.strip() or None}
    dep["pending"] = None
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "publish.activate",
                 f"اتفعّل «{p['name']}» (sha {p['sha256'][:12]}).",
                 f"Activated {p['name']}.",
                 why_ar="الموديل القديم محفوظ، والرجوع له خطوة واحدة («ارجع للموديل السابق»).",
                 why_en="The previous model is kept; rollback is one step.",
                 outputs={"sha": p["sha256"][:12], "skip_shadow_reason": skip_shadow_reason.strip() or None})
    return dep["current"]


def rollback(slug: str, reason: str = "") -> dict[str, Any]:
    dep = get_deployment(slug)
    if not dep["history"]:
        raise PublishError("There is no previous model to go back to.")
    prev = dep["history"].pop()
    if prev.get("folder") and not (project_dir(slug) / prev["folder"]).is_dir():
        raise PublishError(f"The previous model's folder is missing: {prev['folder']}")
    now = _now()
    if dep["current"]:
        dep["history"].append({**dep["current"], "rolled_back_at": now, "rollback_reason": reason.strip() or None})
    dep["current"] = {**prev, "activated_at": now, "activated_by": current_user(), "restored": True}
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "publish.rollback",
                 f"رجعنا للموديل السابق «{prev['name']}» (sha {prev['sha256'][:12]})."
                 + (f" السبب: {reason.strip()}" if reason.strip() else ""),
                 f"Rolled back to {prev['name']}.", outputs={"sha": prev["sha256"][:12], "reason": reason.strip()})
    return dep["current"]


def adopt_current(slug: str, model_id: str, note: str = "") -> dict[str, Any]:
    """Record the model already running at the bakery (e.g. r2) as current, so the gate has a baseline.

    It is not copied as a publish: it was deployed before the studio. Its licence is recorded as is.
    """
    dep = get_deployment(slug)
    if dep["current"]:
        raise PublishError("A current model is already recorded.")
    entry = registry.get_model(model_id)
    dep["current"] = {"model_id": entry["id"], "name": entry["name"], "sha256": entry["sha256"], "folder": None,
                      "gate_report": None, "adopted": True, "note": note.strip(), "activated_at": _now(),
                      "activated_by": current_user()}
    _save_deployment(slug, dep)
    activity.log(project_dir(slug), "publish.adopt",
                 f"اتسجّل «{entry['name']}» على إنه الموديل الشغال حاليًا في المخبز (للمقارنة).",
                 f"Recorded {entry['name']} as the model in use.", outputs={"sha": entry["sha256"][:12]})
    return dep["current"]

