"""HTTP API and static UI. Binds to localhost by default; there are no accounts in v1."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .. import __version__, ingest, projects, sources
from ..config import workspace_dir
from ..explain import activity
from ..ingest.media import IMAGE_EXT, VIDEO_EXT
from ..sources.secrets import SecretError

USER_ERRORS = (projects.ProjectError, sources.SourceError, ingest.IngestError, SecretError)


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
        return {"version": __version__, "workspace": str(workspace_dir()),
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
        if not 1 <= seconds <= 3600:
            raise HTTPException(400, "Recording length must be between 1 second and 1 hour.")
        clip = sources.record(slug, source_id, seconds)
        try:
            return ingest.add_video(slug, clip, angle_id=body.get("angle_id") or "angle-1", source_id=source_id)
        except ingest.IngestError:
            clip.unlink(missing_ok=True)
            clip.with_suffix(".timestamps.json").unlink(missing_ok=True)
            raise

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

    app.mount("/", StaticFiles(directory=_web_dir(), html=True), name="web")
    return app


app = create_app()
