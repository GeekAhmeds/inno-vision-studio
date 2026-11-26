"""HTTP API and static UI. Binds to localhost by default; there are no accounts in v1."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

import cv2
from inno_count.engine import frame_at

from .. import __version__, datasets, ingest, jobs, prelabel, projects, registry, review, sample, sources, train, truth
from ..harness import run as harness_run
from ..truth import proxy
from ..config import current_user, workspace_dir
from ..explain import activity
from ..ingest.media import IMAGE_EXT, VIDEO_EXT
from ..sources.secrets import SecretError

USER_ERRORS = (projects.ProjectError, sources.SourceError, ingest.IngestError, SecretError, truth.TruthError,
               harness_run.HarnessError, registry.RegistryError, sample.SampleError, prelabel.PrelabelError,
               review.ReviewError, datasets.DatasetError, train.TrainError)
jobs.USER_ERRORS = USER_ERRORS


def _web_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "web", here.parents[1] / "_web"):
        if (candidate / "index.html").exists():
            return candidate
    raise RuntimeError("UI files not found.")


def _safe_name(name: str | None, allowed: set[str]) -> str:
    name = PurePosixPath((name or "").replace("\\", "/")).name
    if not name or Path(name).suffix.lower() not in allowed:
        raise HTTPException(400, f"File type not accepted: {name or '(no name)'}")
    return name


def _save(upload: UploadFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        shutil.copyfileobj(upload.file, out)


def create_app() -> FastAPI:
    app = FastAPI(title="Inno Vision Studio", version=__version__)

    def user_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for error in USER_ERRORS:
        app.add_exception_handler(error, user_error)

    # ------------------------------------------------------------ general

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {"version": __version__, "workspace": str(workspace_dir()), "user": current_user(),
                "templates": projects.TEMPLATES, "source_kinds": sources.KINDS}

    @app.get("/api/usb-cameras")
    def usb_cameras() -> list[dict[str, Any]]:
        return sources.list_usb_cameras()

    # ------------------------------------------------------------ projects

    @app.get("/api/projects")
    def project_list() -> list[dict[str, Any]]:
        return projects.list_projects()

    @app.post("/api/projects")
    def project_create(body: dict = Body(...)) -> dict[str, Any]:
        return projects.create_project(body.get("name", ""), body.get("product", ""),
                                       body.get("template", "count_line"))

    @app.post("/api/projects/import")
    def project_import(file: UploadFile = File(...)) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "import.zip"
            _save(file, archive)
            return projects.import_project(archive)

    @app.get("/api/projects/{slug}")
    def project_get(slug: str) -> dict[str, Any]:
        data = projects.get_project(slug)
        data["stats"] = projects.store.stats(projects.project_dir(slug))
        data["sources"] = sources.list_sources(slug)
        return data

    @app.patch("/api/projects/{slug}")
    def project_patch(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        if "name" in body:
            projects.rename_project(slug, body["name"])
        if "archived" in body:
            projects.archive_project(slug, bool(body["archived"]))
        return projects.get_project(slug)

    @app.post("/api/projects/{slug}/duplicate")
    def project_duplicate(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        return projects.duplicate_project(slug, body.get("name", ""))

    @app.delete("/api/projects/{slug}")
    def project_delete(slug: str, confirm: str = "") -> dict[str, bool]:
        projects.delete_project(slug, confirm)
        return {"deleted": True}

    @app.get("/api/projects/{slug}/export")
    def project_export(slug: str) -> FileResponse:
        tmp = Path(tempfile.mkdtemp(prefix="ivs-export-"))
        archive = projects.export_project(slug, tmp)
        return FileResponse(archive, filename=archive.name, media_type="application/zip",
                            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))

    @app.post("/api/projects/{slug}/classes")
    def class_add(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        return projects.add_class(slug, body.get("display_name", ""))

    @app.get("/api/projects/{slug}/activity")
    def activity_list(slug: str, limit: int = 100) -> list[dict[str, Any]]:
        return activity.read(projects.project_dir(slug), limit=limit)

    @app.get("/api/projects/{slug}/file/{rel:path}")
    def project_file(slug: str, rel: str) -> FileResponse:
        """Serve pictures from inside the project (previews, reference frames, uploaded images)."""
        folder = projects.project_dir(slug).resolve()
        target = (folder / rel).resolve()
        if folder not in target.parents or target.suffix.lower() not in IMAGE_EXT or not target.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(target)

    # ------------------------------------------------------------ sources

    @app.get("/api/projects/{slug}/sources")
    def source_list(slug: str) -> list[dict[str, Any]]:
        return sources.list_sources(slug)

    @app.post("/api/projects/{slug}/sources")
    def source_add(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        return sources.add_source(slug, body.get("kind", ""), body.get("name", ""), url=body.get("url", ""),
                                  device=body.get("device"), path=body.get("path", ""),
                                  password=body.get("password") or None)

    @app.patch("/api/projects/{slug}/sources/{source_id}")
    def source_login(slug: str, source_id: str, body: dict = Body(...)) -> dict[str, Any]:
        return sources.update_login(slug, source_id, body.get("url") or None, body.get("user"), body.get("password") or None)

    @app.post("/api/projects/{slug}/sources/{source_id}/find-path")
    def source_find_path(slug: str, source_id: str) -> dict[str, Any]:
        return sources.find_stream_paths(slug, source_id)

    @app.post("/api/projects/{slug}/sources/{source_id}/use-path")
    def source_use_path(slug: str, source_id: str, body: dict = Body(...)) -> dict[str, Any]:
        return sources.use_stream_path(slug, source_id, body.get("url_path", ""))

    @app.delete("/api/projects/{slug}/sources/{source_id}")
    def source_remove(slug: str, source_id: str) -> dict[str, bool]:
        sources.remove_source(slug, source_id)
        return {"deleted": True}

    @app.post("/api/projects/{slug}/sources/{source_id}/test")
    def source_test(slug: str, source_id: str) -> dict[str, Any]:
        return sources.test_connection(slug, source_id)

    @app.post("/api/projects/{slug}/sources/{source_id}/record")
    def source_record(slug: str, source_id: str, body: dict = Body(...)) -> dict[str, Any]:
        seconds = float(body.get("seconds", 60))
        if not 1 <= seconds <= 4 * 3600:
            raise HTTPException(400, "Recording length must be between 1 second and 4 hours.")
        source = sources.get_source(slug, source_id)
        angle = body.get("angle_id") or None

        def work(ctx: jobs.Context) -> dict[str, Any]:
            def progress(fraction: float, message: str) -> None:
                try:
                    ctx.progress(fraction, message)
                except jobs.Cancelled:
                    pass   # "cancel" stops the recording; what was recorded is kept and registered

            media = sources.record_and_register(slug, source_id, seconds, angle,
                                                progress=progress, should_stop=ctx.cancelled)
            return {"media_id": media["id"], "width": media["width"], "height": media["height"],
                    "frames": media["frames"], "duration_s": media["duration_s"], "angle_id": media.get("angle_id")}

        return jobs.manager.submit(slug, "record", f"تسجيل {int(seconds)} ث من «{source['name']}»", work,
                                   lane="capture").to_dict()

    # ------------------------------------------------------------ media

    @app.get("/api/projects/{slug}/media")
    def media_list(slug: str, kind: str | None = None) -> list[dict[str, Any]]:
        return ingest.list_media(slug, kind)

    @app.post("/api/projects/{slug}/media/video-link")
    def video_link(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        """Register a video that is already on this machine, without copying it."""
        return ingest.add_video(slug, body.get("path", ""), mode=body.get("mode", "link"),
                                angle_id=body.get("angle_id") or "angle-1")

    @app.post("/api/projects/{slug}/media/video")
    def video_upload(slug: str, file: UploadFile = File(...), angle_id: str = Form("angle-1")) -> dict[str, Any]:
        folder = projects.project_dir(slug)
        name = _safe_name(file.filename, VIDEO_EXT)
        target = ingest.media._free_name(folder / "media" / "videos", name)
        _save(file, target)
        try:
            return ingest.add_video(slug, target, angle_id=angle_id or "angle-1")
        except ingest.IngestError:
            target.unlink(missing_ok=True)
            raise

    @app.post("/api/projects/{slug}/media/images")
    def images_upload(slug: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        with tempfile.TemporaryDirectory() as tmp:
            for i, upload in enumerate(files):
                _save(upload, Path(tmp) / f"{i:06d}" / _safe_name(upload.filename, IMAGE_EXT))
            return ingest.add_images(slug, [tmp])

    @app.post("/api/projects/{slug}/media/product-folders")
    def product_folders_upload(slug: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """The browser sends a chosen folder as files named `<root>/<product>/<picture>`."""
        projects.project_dir(slug)
        with tempfile.TemporaryDirectory() as tmp:
            root, kept = Path(tmp), 0
            for upload in files:
                parts = [p for p in PurePosixPath((upload.filename or "").replace("\\", "/")).parts
                         if p not in ("", ".", "..", "/")]
                if len(parts) < 3 or Path(parts[-1]).suffix.lower() not in IMAGE_EXT:
                    continue
                _save(upload, root / parts[-2] / f"{kept:06d}" / parts[-1])
                kept += 1
            if not kept:
                raise HTTPException(400, "Choose a folder that contains one sub-folder of pictures per product.")
            return ingest.add_product_folders(slug, root)

    @app.post("/api/projects/{slug}/media/product-folders-path")
    def product_folders_path(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        return ingest.add_product_folders(slug, body.get("path", ""))

    # ------------------------------------------------------------ counting setup

    @app.get("/api/projects/{slug}/counting")
    def counting_get(slug: str) -> dict[str, Any]:
        folder = projects.project_dir(slug)
        angles = []
        for meta_file in sorted((folder / "sources" / "angles").glob("*/angle.json")):
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["reference"] = (meta_file.parent / "reference.jpg").relative_to(folder).as_posix()
            angles.append(meta)
        return {"config": projects.get_counting(slug), "angles": angles}

    @app.put("/api/projects/{slug}/counting")
    def counting_put(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        return projects.set_counting(slug, body)

    @app.get("/api/projects/{slug}/media/{media_id}/frame")
    def media_frame(slug: str, media_id: int, t: float = 0.0, width: int = 0,
                    mark_x: float | None = None, mark_y: float | None = None) -> Response:
        path = harness_run.media_path(slug, media_id)
        frame = frame_at(path, t)
        if frame is None:
            raise HTTPException(404, "No frame at this time.")
        line = projects.get_counting(slug)["line"]
        if line:
            cv2.line(frame, (int(line[0]), int(line[1])), (int(line[2]), int(line[3])), (0, 200, 255), 2)
        if mark_x is not None and mark_y is not None:
            cv2.circle(frame, (int(mark_x), int(mark_y)), max(12, frame.shape[1] // 40), (255, 0, 255), 3)
        if 0 < width < frame.shape[1]:
            frame = cv2.resize(frame, (width, int(frame.shape[0] * width / frame.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    # ------------------------------------------------------------ truth

    def _clip_with_proxy(slug: str, clip_id: int) -> dict[str, Any]:
        clip = truth.get_clip(slug, clip_id)
        clip["proxy"] = proxy.status(projects.project_dir(slug), clip_id)
        return clip

    @app.get("/api/projects/{slug}/truth")
    def truth_list(slug: str) -> dict[str, Any]:
        return {"conditions": truth.CONDITIONS, "clips": truth.list_clips(slug),
                "min_per_condition": harness_run.MIN_CROSSINGS_PER_CONDITION}

    @app.post("/api/projects/{slug}/truth")
    def truth_create(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        clip = truth.create_clip(slug, int(body.get("media_id", 0)), body.get("start_s", 0), body.get("end_s", 0),
                                 body.get("condition", ""), body.get("note", ""))
        proxy.start(harness_run.media_path(slug, clip["media_id"]), projects.project_dir(slug), clip["id"],
                    clip["start_s"], clip["end_s"])
        return _clip_with_proxy(slug, clip["id"])

    @app.get("/api/projects/{slug}/truth/{clip_id}")
    def truth_get(slug: str, clip_id: int) -> dict[str, Any]:
        clip = _clip_with_proxy(slug, clip_id)
        if clip["proxy"]["state"] == "missing":  # e.g. after an import: build it again
            proxy.start(harness_run.media_path(slug, clip["media_id"]), projects.project_dir(slug), clip_id,
                        clip["start_s"], clip["end_s"])
            clip["proxy"] = {"state": "building", "progress": 0.0}
        return clip

    @app.get("/api/projects/{slug}/truth/{clip_id}/proxy.webm")
    def truth_proxy(slug: str, clip_id: int) -> FileResponse:
        path = proxy.proxy_dir(projects.project_dir(slug), clip_id) / "proxy.webm"
        if proxy.status(projects.project_dir(slug), clip_id).get("state") != "ready" or not path.is_file():
            raise HTTPException(404, "The browser copy is not ready.")
        return FileResponse(path, media_type="video/webm")

    @app.get("/api/projects/{slug}/truth/{clip_id}/stamps")
    def truth_stamps(slug: str, clip_id: int) -> list[float]:
        path = proxy.proxy_dir(projects.project_dir(slug), clip_id) / "stamps.json"
        if not path.is_file():
            raise HTTPException(404, "Not ready.")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.delete("/api/projects/{slug}/truth/{clip_id}")
    def truth_delete(slug: str, clip_id: int) -> dict[str, bool]:
        truth.delete_clip(slug, clip_id)
        shutil.rmtree(proxy.proxy_dir(projects.project_dir(slug), clip_id), ignore_errors=True)
        return {"deleted": True}

    @app.post("/api/projects/{slug}/truth/{clip_id}/counts")
    def truth_count_start(slug: str, clip_id: int) -> dict[str, Any]:
        return truth.start_count(slug, clip_id)

    @app.put("/api/projects/{slug}/truth-counts/{count_id}")
    def truth_count_save(slug: str, count_id: int, body: dict = Body(...)) -> dict[str, Any]:
        return truth.save_count(slug, count_id, body.get("taps", []), bool(body.get("finished")))

    @app.get("/api/projects/{slug}/truth/{clip_id}/reconcile")
    def truth_reconcile(slug: str, clip_id: int) -> dict[str, Any]:
        return truth.reconcile(slug, clip_id)

    @app.post("/api/projects/{slug}/truth/{clip_id}/resolve")
    def truth_resolve(slug: str, clip_id: int, body: dict = Body(default={})) -> dict[str, Any]:
        return truth.resolve(slug, clip_id, body.get("decisions"))

    @app.post("/api/projects/{slug}/truth/{clip_id}/lock")
    def truth_lock(slug: str, clip_id: int) -> dict[str, Any]:
        return truth.lock_clip(slug, clip_id)

    # ------------------------------------------------------------ harness

    @app.post("/api/projects/{slug}/harness")
    def harness_post(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        model_path = (body.get("model_path") or "").strip()
        if not model_path:
            raise harness_run.HarnessError("Choose a model file (.onnx) to measure.")
        if not Path(model_path).expanduser().is_file():
            raise harness_run.HarnessError(f"Model file not found: {model_path}")
        fps_list = [float(f) for f in (body.get("fps_list") or [10])]
        tracker, latency = body.get("tracker", "bytetrack"), float(body.get("latency_s") or 0)
        clip_ids = body.get("clip_ids") or None

        def work(ctx: jobs.Context) -> dict[str, Any]:
            ctx.progress(0.01, "تشغيل الموديل على مقاطع المسطرة")
            report = harness_run.run_harness(slug, model_path=Path(model_path).expanduser(), clip_ids=clip_ids,
                                             fps_list=fps_list, tracker=tracker, latency_s=latency)
            return {"report_dir": report.get("report_dir"), "runs": [
                {"fps": r["fps"], "overall": r["overall"], "conditions": r["conditions"]} for r in report["runs"]]}

        return jobs.manager.submit(slug, "harness", "قياس الموديل على المسطرة", work).to_dict()

    @app.get("/api/projects/{slug}/reports")
    def reports_list(slug: str) -> list[dict[str, Any]]:
        folder = projects.project_dir(slug) / "reports"
        out = []
        for d in sorted(folder.glob("harness_*"), reverse=True):
            data = json.loads((d / "report.json").read_text(encoding="utf-8")) if (d / "report.json").exists() else {}
            first = (data.get("runs") or [{}])[0].get("overall", {})
            out.append({"name": d.name, "created_at": data.get("created_at"), "model_sha": data.get("model_sha", "")[:12],
                        "tracker": data.get("tracker"), "fps": [r["fps"] for r in data.get("runs", [])],
                        "truth": first.get("truth"), "misses": first.get("misses"), "extras": first.get("extras")})
        return out

    @app.get("/api/projects/{slug}/reports/{name}/report.html")
    def report_html(slug: str, name: str) -> FileResponse:
        if not name.startswith("harness_") or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(404, "Not found")
        path = projects.project_dir(slug) / "reports" / name / "report.html"
        if not path.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(path, media_type="text/html")

    # ------------------------------------------------------------ jobs

    @app.get("/api/projects/{slug}/jobs")
    def jobs_list(slug: str) -> list[dict[str, Any]]:
        return jobs.manager.list(slug)

    @app.get("/api/projects/{slug}/jobs/{job_id}")
    def job_get(slug: str, job_id: str) -> dict[str, Any]:
        try:
            return jobs.manager.get(slug, job_id)
        except KeyError:
            raise HTTPException(404, "Job not found") from None

    @app.post("/api/projects/{slug}/jobs/{job_id}/cancel")
    def job_cancel(slug: str, job_id: str) -> dict[str, Any]:
        try:
            return jobs.manager.cancel(job_id).to_dict()
        except KeyError:
            raise HTTPException(404, "Job not found (or it already ended before the studio restarted)") from None

    # ------------------------------------------------------------ helper models (workspace-wide)

    @app.get("/api/models")
    def models_list() -> dict[str, Any]:
        return {"policy": registry.policy(), "roles": registry.ROLES, "models": registry.list_models()}

    @app.post("/api/models")
    def models_add(body: dict = Body(...)) -> dict[str, Any]:
        return registry.add_model(body.get("path", ""), body.get("name", ""), body.get("role", "detector"),
                                  body.get("licence", ""), body.get("note", ""))

    @app.delete("/api/models/{model_id}")
    def models_remove(model_id: str) -> dict[str, bool]:
        registry.remove_model(model_id)
        return {"deleted": True}

    @app.put("/api/models-policy")
    def models_policy(body: dict = Body(...)) -> dict[str, str]:
        return {"policy": registry.set_policy(body.get("policy", ""))}

    # ------------------------------------------------------------ labelling

    @app.post("/api/projects/{slug}/sample")
    def sample_post(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        n = int(body["n"]) if body.get("n") is not None else 200
        every = float(body["every_s"]) if body.get("every_s") is not None else 5.0
        share = float(body.get("negative_share") if body.get("negative_share") is not None else 0.15)
        ids = body.get("media_ids") or None
        if not 1 <= n <= 5000 or every < 0.2 or not 0 <= share <= 0.5:
            raise sample.SampleError("Frames 1–5000, scan every ≥ 0.2 s, negatives 0–50 %.")
        return jobs.manager.submit(slug, "sample", f"اختيار {n} صورة للتسمية", lambda ctx: sample.run_sample(
            slug, ids, n, every, share, progress=ctx.progress)).to_dict()

    @app.post("/api/projects/{slug}/prelabel")
    def prelabel_post(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        entry = registry.get_model(body.get("model_id", ""))
        only = bool(body.get("only_unreviewed", True))
        return jobs.manager.submit(slug, "prelabel", f"تسمية مبدئية بـ «{entry['name']}»", lambda ctx: prelabel.run_prelabel(
            slug, entry["id"], only_unreviewed=only, progress=ctx.progress)).to_dict()

    @app.post("/api/projects/{slug}/bakeoff")
    def bakeoff_post(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        ids = [registry.get_model(i)["id"] for i in body.get("model_ids") or []]
        if not ids:
            raise prelabel.PrelabelError("Choose at least one detector to compare.")
        return jobs.manager.submit(slug, "bakeoff", "مقارنة الموديلات المساعدة", lambda ctx: prelabel.run_bakeoff(
            slug, ids, progress=ctx.progress)).to_dict()

    # ------------------------------------------------------------ datasets and training (P4)

    @app.get("/api/projects/{slug}/training")
    def training_get(slug: str) -> dict[str, Any]:
        projects.project_dir(slug)
        return {"snapshots": datasets.list_snapshots(slug), "train": train.available(), "sizes": list(train.SIZES),
                "review": review.stats(slug)}

    @app.post("/api/projects/{slug}/datasets")
    def datasets_post(slug: str, body: dict = Body(default={})) -> dict[str, Any]:
        projects.project_dir(slug)
        block_s = float(body.get("block_s") or 600)
        seed = int(body.get("seed") or 0)
        return jobs.manager.submit(slug, "dataset", "تجهيز نسخة بيانات للتدريب", lambda ctx: datasets.build_snapshot(
            slug, block_s=block_s, seed=seed, progress=ctx.progress)).to_dict()

    @app.post("/api/projects/{slug}/train")
    def train_post(slug: str, body: dict = Body(...)) -> dict[str, Any]:
        projects.project_dir(slug)
        snap = str(body.get("snapshot") or "")
        datasets.snapshot_dir(slug, snap)
        size = str(body.get("size") or "nano")
        args = dict(size=size, epochs=int(body.get("epochs") or 50), batch_size=int(body.get("batch_size") or 4),
                    grad_accum=int(body.get("grad_accum") or 4), device=str(body.get("device") or "auto"))
        if not train.available()["installed"]:
            raise train.TrainError("Training is not installed on this computer. Run: " + train.available()["how"])
        return jobs.manager.submit(slug, "train", f"تدريب RF-DETR {size} على {snap}", lambda ctx: train.run_training(
            slug, snap, progress=ctx.progress, cancelled=ctx.cancelled, **args)).to_dict()

    @app.get("/api/projects/{slug}/review")
    def review_overview(slug: str) -> dict[str, Any]:
        return {"stats": review.stats(slug), "queue": review.queue(slug), "classes": review.class_ids(slug),
                "blind_audit": review.blind_audit(slug)}

    @app.get("/api/projects/{slug}/review/{media_id}")
    def review_get(slug: str, media_id: int) -> dict[str, Any]:
        return review.get_frame(slug, media_id)

    @app.put("/api/projects/{slug}/review/{media_id}")
    def review_put(slug: str, media_id: int, body: dict = Body(...)) -> dict[str, Any]:
        return review.confirm(slug, media_id, body.get("boxes", []), bool(body.get("blind")))

    @app.post("/api/projects/{slug}/review/{media_id}/reopen")
    def review_reopen(slug: str, media_id: int) -> dict[str, Any]:
        return review.reopen(slug, media_id)

    app.mount("/", StaticFiles(directory=_web_dir(), html=True), name="web")
    return app


app = create_app()
