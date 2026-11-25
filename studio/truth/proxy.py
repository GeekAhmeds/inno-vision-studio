"""Browser copy of a truth clip, for the counting page.

Camera recordings are often H.265 or MPEG-4 Part 2, which browsers do not play. Each truth clip is
re-encoded once to VP8 WebM (written by OpenCV, no extra install), at most 960 px wide, in a
background thread. A list of the source timestamp of every proxy frame is kept, so a tap at proxy
time p is stored at the exact source time. If VP8 is not available, the page falls back to showing
single frames from the server.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import cv2

from inno_count.engine import read_video

MAX_WIDTH = 960
_jobs: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def proxy_dir(project: Path, clip_id: int) -> Path:
    return project / "truth" / f"clip_{clip_id}"


def status(project: Path, clip_id: int) -> dict[str, Any]:
    path = proxy_dir(project, clip_id) / "proxy.json"
    if not path.exists():
        return {"state": "missing"}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_status(folder: Path, data: dict[str, Any]) -> None:
    tmp = folder / "proxy.json.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(folder / "proxy.json")


def build(source: Path, folder: Path, start_s: float, end_s: float) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    cap.release()
    writer, stamps, scale = None, [], 1.0
    total = max(1, int((end_s - start_s) * fps))
    out = folder / "proxy.webm"
    try:
        for ts, frame in read_video(source, start_s, end_s):
            if writer is None:
                h, w = frame.shape[:2]
                scale = min(1.0, MAX_WIDTH / w)
                size = (int(round(w * scale)) // 2 * 2, int(round(h * scale)) // 2 * 2)
                writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"VP80"), fps, size)
                if not writer.isOpened():
                    raise RuntimeError("This OpenCV build cannot write VP8 video.")
                _write_status(folder, {"state": "building", "progress": 0.0})
            small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA) if scale < 1 else frame
            writer.write(small)
            stamps.append(round(ts, 4))
            if len(stamps) % 200 == 0:
                _write_status(folder, {"state": "building", "progress": round(min(len(stamps) / total, 0.99), 3)})
    finally:
        if writer is not None:
            writer.release()
    if not stamps:
        raise RuntimeError("No frame could be read in this time range.")
    (folder / "stamps.json").write_text(json.dumps(stamps), encoding="utf-8")
    data = {"state": "ready", "fps": fps, "scale": 1.0 / scale, "frames": len(stamps), "file": "proxy.webm"}
    _write_status(folder, data)
    return data


def start(source: Path, project: Path, clip_id: int, start_s: float, end_s: float) -> None:
    folder = proxy_dir(project, clip_id)
    key = str(folder)
    with _lock:
        if key in _jobs and _jobs[key].is_alive():
            return
        folder.mkdir(parents=True, exist_ok=True)
        _write_status(folder, {"state": "building", "progress": 0.0})

        def work() -> None:
            try:
                build(source, folder, start_s, end_s)
            except Exception as exc:  # reported to the page, which then shows single frames
                _write_status(folder, {"state": "failed", "error": str(exc)})

        _jobs[key] = threading.Thread(target=work, daemon=True)
        _jobs[key].start()


def wait(project: Path, clip_id: int, timeout: float = 60.0) -> None:
    job = _jobs.get(str(proxy_dir(project, clip_id)))
    if job:
        job.join(timeout)
