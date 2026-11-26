"""Train a detector on a dataset snapshot (P4), with RF-DETR (Apache 2.0, decision D6).

The training itself runs in a child process (`studio.train_worker`) so torch stays out of the
studio. The result is exported to ONNX, given a sidecar, and added to the model registry with its
licence and the snapshot it was trained on. It is then measured like any other model: the harness
on truth clips decides, never the validation mAP alone.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import registry
from .datasets import snapshot_dir
from .explain import activity
from .projects import get_project, project_dir

SIZES = ("nano", "small", "medium", "large")
LICENCE = "Apache-2.0"


class TrainError(Exception):
    """A problem the owner can understand and fix."""


def available() -> dict[str, Any]:
    """Is the training extra installed, and is there a GPU? Without importing torch in the studio."""
    have = all(importlib.util.find_spec(m) is not None for m in ("rfdetr", "torch", "pytorch_lightning"))
    return {"installed": have, "how": 'uv sync --extra train   (or: pip install "inno-vision-studio[train]")'}


def epochs_done(out: Path) -> tuple[int, float | None]:
    """Epochs finished and the best validation mAP50:95 so far, from Lightning's metrics.csv."""
    best, epochs = None, set()
    for f in out.rglob("metrics.csv"):
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    m = row.get("val/mAP_50_95") or row.get("val/mAP_50_95_ema")
                    if m:
                        epochs.add(row.get("epoch"))
                        best = max(best or 0.0, float(m))
        except (OSError, ValueError):
            continue
    return len(epochs), best


def run_training(slug: str, snap_id: str, size: str = "nano", epochs: int = 50, batch_size: int = 4,
                 grad_accum: int = 4, device: str = "auto", progress: Callable[[float, str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, worker: list[str] | None = None) -> dict[str, Any]:
    if size not in SIZES:
        raise TrainError(f"Size must be one of: {', '.join(SIZES)}.")
    if not 1 <= epochs <= 1000 or not 1 <= batch_size <= 64 or not 1 <= grad_accum <= 64:
        raise TrainError("Epochs 1–1000, batch 1–64, accumulation 1–64.")
    data = snapshot_dir(slug, snap_id)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    if worker is None and not available()["installed"]:
        raise TrainError("Training is not installed on this computer. Run: " + available()["how"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"train-{stamp}"
    out = project_dir(slug) / "models" / run_id
    out.mkdir(parents=True, exist_ok=True)
    cmd = (worker or [sys.executable, "-m", "studio.train_worker"]) + [
        "--dataset", str(data), "--out", str(out), "--size", size, "--epochs", str(epochs),
        "--batch-size", str(batch_size), "--grad-accum", str(grad_accum), "--device", device]
    settings = {"snapshot": snap_id, "size": size, "epochs": epochs, "batch_size": batch_size,
                "grad_accum": grad_accum, "device": device}
    (out / "run.json").write_text(json.dumps({"id": run_id, **settings}, indent=1), encoding="utf-8")
    log = (out / "train.log").open("w", encoding="utf-8")
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"})
    result_line = None
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        last_poll = 0.0
        for line in proc.stdout:
            log.write(line)
            tail = (tail + [line.rstrip()])[-30:]
            if line.startswith("RESULT "):
                result_line = line[len("RESULT "):]
            if cancelled and cancelled():
                proc.terminate()
                break
            now = time.monotonic()
            if progress and now - last_poll > 2.0:
                last_poll = now
                done, best = epochs_done(out)
                mins = int((now - started) // 60)
                msg = f"تدريب RF-DETR {size}: {done}/{epochs} دورة · {mins} دقيقة"
                if best is not None:
                    msg += f" · أفضل mAP على التحقق {best:.3f}"
                progress(min(0.95, done / epochs), msg)
        code = proc.wait()
    finally:
        log.close()
        if proc.poll() is None:
            proc.kill()
    if cancelled and cancelled():
        raise TrainError("Training was stopped.")
    if code != 0 or result_line is None:
        raise TrainError("Training failed. Last lines of the log:\n" + "\n".join(tail[-12:]))
    result = json.loads(result_line)
    onnx_src = Path(result["onnx"])
    if not onnx_src.is_file():
        raise TrainError(f"Training finished but the ONNX file is missing: {onnx_src}")
    model_path = out / "model.onnx"
    shutil.copy2(onnx_src, model_path)
    done, best = epochs_done(out)
    meta = get_project(slug)
    sidecar = {"format": "rfdetr", "names": manifest["classes"], "imgsz": result.get("resolution") or None,
               "conf": 0.4, "licence": LICENCE, "trained_on": snap_id, "project": slug, "run": run_id,
               "settings": settings, "epochs_done": done, "val_map_50_95": best,
               "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "model.json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=1), encoding="utf-8")
    entry = registry.add_model(model_path, f"{meta['name']} · RF-DETR {size} · {stamp[:13]}", "detector", LICENCE,
                               note=f"trained on {snap_id} ({run_id})")
    summary = {"run": run_id, "model": entry, "epochs_done": done, "val_map_50_95": best,
               "minutes": round((time.monotonic() - started) / 60, 1), **settings}
    activity.log(project_dir(slug), "train.done",
                 f"اتدرّب موديل RF-DETR {size} على {snap_id} ({done} دورة) واتسجّل في الموديلات.",
                 f"RF-DETR {size} trained on {snap_id} and registered.",
                 why_ar="رقم التحقق (mAP) مش هو الحكم: الحكم هو قياس الموديل على المسطرة (العدّ البشري).",
                 why_en="Validation mAP is not the verdict; the harness on truth clips is.",
                 outputs={"model_id": entry["id"], "val_map_50_95": best, "epochs_done": done})
    return summary
