"""Background jobs with progress and cancel (prompt §4).

Long steps (sampling 25 h of video, pre-labelling thousands of frames) must not block the page and
must survive a page refresh. Jobs run one at a time per lane: the "compute" lane holds everything
that uses the GPU or the CPU heavily (8 GB of GPU cannot hold two models); the "capture" lane holds
camera recordings, which must start on time and never wait behind a long computation. Each job is written to `<project>/jobs/<id>.json`, so the
history survives a restart; a job that was running when the studio stopped shows as interrupted.
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .projects import project_dir

QUEUED, RUNNING, DONE, FAILED, CANCELLED, INTERRUPTED = "queued", "running", "done", "failed", "cancelled", "interrupted"
USER_ERRORS: tuple[type[Exception], ...] = ()   # filled by the API: shown as is, without a traceback


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    project: str
    kind: str
    title_ar: str
    state: str = QUEUED
    progress: float = 0.0
    message_ar: str = ""
    result: Any = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Context:
    """Given to the job function: report progress, and check for cancel."""

    def __init__(self, job: Job, manager: "JobManager"):
        self.job, self._manager = job, manager
        self._cancel = threading.Event()

    def progress(self, fraction: float, message_ar: str = "") -> None:
        if self._cancel.is_set():
            raise Cancelled()
        self.job.progress = round(max(0.0, min(1.0, fraction)), 4)
        if message_ar:
            self.job.message_ar = message_ar
        self._manager._save(self.job)

    def cancelled(self) -> bool:
        return self._cancel.is_set()


class JobManager:
    def __init__(self) -> None:
        self._queues: dict[str, queue.Queue[tuple[Job, Callable[[Context], Any], Context]]] = {}
        self._workers: dict[str, threading.Thread | None] = {}
        self._contexts: dict[str, Context] = {}
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()

    def _dir(self, slug: str) -> Path:
        d = project_dir(slug) / "jobs"
        d.mkdir(exist_ok=True)
        return d

    def _save(self, job: Job) -> None:
        with self._save_lock:
            try:
                path = self._dir(job.project) / f"{job.id}.json"
                tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
                tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, default=str), encoding="utf-8")
                tmp.replace(path)
            except Exception:  # the project was deleted while the job ran: nothing to record
                return

    def submit(self, slug: str, kind: str, title_ar: str, fn: Callable[[Context], Any], lane: str = "compute") -> Job:
        job = Job(uuid.uuid4().hex[:12], slug, kind, title_ar)
        ctx = Context(job, self)
        with self._lock:
            self._jobs[job.id] = job
            self._contexts[job.id] = ctx
            self._save(job)
            q = self._queues.setdefault(lane, queue.Queue())
            q.put((job, fn, ctx))
            worker = self._workers.get(lane)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._run, args=(lane,), daemon=True, name=f"studio-jobs-{lane}")
                self._workers[lane] = worker
                worker.start()
        return job

    def _run(self, lane: str) -> None:
        q = self._queues[lane]
        while True:
            try:
                job, fn, ctx = q.get(timeout=5)
            except queue.Empty:
                with self._lock:
                    if q.empty():
                        self._workers[lane] = None
                        return
                continue
            if ctx.cancelled():
                job.state, job.finished_at = CANCELLED, _now()
                self._save(job)
                continue
            job.state, job.started_at = RUNNING, _now()
            self._save(job)
            try:
                job.result = fn(ctx)
                job.state, job.progress = DONE, 1.0
            except Cancelled:
                job.state = CANCELLED
            except USER_ERRORS as exc:
                job.state, job.error = FAILED, str(exc)
            except Exception as exc:  # unexpected: keep the traceback for the developer
                job.state, job.error = FAILED, f"{exc}\n{traceback.format_exc(limit=5)}"
            job.finished_at = _now()
            self._save(job)

    def cancel(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        self._contexts[job_id]._cancel.set()
        if job.state == QUEUED:
            job.state = CANCELLED
            self._save(job)
        return job

    def get(self, slug: str, job_id: str) -> dict[str, Any]:
        if job_id in self._jobs and self._jobs[job_id].project == slug:
            return self._jobs[job_id].to_dict()
        path = self._dir(slug) / f"{job_id}.json"
        if not path.exists():
            raise KeyError(job_id)
        return self._from_disk(path)

    def _from_disk(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["state"] in (QUEUED, RUNNING) and data["id"] not in self._jobs:
            data["state"] = INTERRUPTED   # the studio stopped while it ran
        return data

    def list(self, slug: str, limit: int = 20) -> list[dict[str, Any]]:
        files = sorted(self._dir(slug).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out = []
        for path in files:
            job_id = path.stem
            out.append(self._jobs[job_id].to_dict() if job_id in self._jobs else self._from_disk(path))
        return out

    def wait(self, job_id: str, timeout: float = 60.0) -> Job:
        """For tests and scripts: block until the job leaves the queue."""
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self._jobs[job_id]
            if job.state not in (QUEUED, RUNNING):
                return job
            time.sleep(0.05)
        raise TimeoutError(job_id)


manager = JobManager()
