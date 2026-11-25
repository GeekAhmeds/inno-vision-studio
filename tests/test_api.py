from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from studio.api import create_app

from .conftest import write_image, write_video


@pytest.fixture
def client(workspace):
    return TestClient(create_app())


def test_ui_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Inno Vision Studio" in page.text
    assert client.get("/app.js").status_code == 200


def test_project_lifecycle_over_http(client):
    created = client.post("/api/projects", json={"name": "Bread", "product": "bread", "template": "count_per_type"})
    assert created.status_code == 200 and created.json()["slug"] == "bread"
    assert client.post("/api/projects", json={"name": ""}).status_code == 400
    assert client.patch("/api/projects/bread", json={"name": "خبز"}).json()["name"] == "خبز"
    assert client.post("/api/projects/bread/classes", json={"display_name": "سياحي كبير"}).json()["id"] == "class_1"
    assert client.get("/api/projects/nope").status_code == 400
    assert client.delete("/api/projects/bread?confirm=oops").status_code == 400
    assert client.delete("/api/projects/bread?confirm=bread").json() == {"deleted": True}
    assert client.get("/api/projects").json() == []


def test_upload_video_then_duplicate_is_refused_and_cleaned_up(client, tmp_path):
    client.post("/api/projects", json={"name": "Bread"})
    data = write_video(tmp_path / "clip.mp4").read_bytes()
    first = client.post("/api/projects/bread/media/video", files={"file": ("clip.mp4", data, "video/mp4")})
    assert first.status_code == 200 and first.json()["frames"] == 30
    second = client.post("/api/projects/bread/media/video", files={"file": ("clip.mp4", data, "video/mp4")})
    assert second.status_code == 400 and "already" in second.json()["detail"]
    from studio import projects
    videos = list((projects.project_dir("bread") / "media" / "videos").iterdir())
    assert [v.name for v in videos] == ["clip.mp4"], "the refused upload must not stay on disk"
    assert client.post("/api/projects/bread/media/video",
                       files={"file": ("evil.exe", b"x", "application/octet-stream")}).status_code == 400


def test_product_folder_upload_as_the_browser_sends_it(client, tmp_path):
    client.post("/api/projects", json={"name": "Shop"})
    files = []
    for i, rel in enumerate(["stock/cans/a.png", "stock/cans/b.png", "stock/معلبات فول/c.png", "stock/readme.txt"]):
        content = b"text" if rel.endswith(".txt") else write_image(tmp_path / f"{i}.png", i).read_bytes()
        files.append(("files", (rel, io.BytesIO(content), "application/octet-stream")))
    result = client.post("/api/projects/shop/media/product-folders", files=files)
    assert result.status_code == 200, result.text
    names = sorted(v["display_name"] for v in result.json()["classes"].values())
    assert names == ["cans", "معلبات فول"]
    assert client.get("/api/projects/shop").json()["stats"]["labels_human"] == 3


def test_path_traversal_in_upload_names_stays_inside(client, tmp_path, workspace):
    client.post("/api/projects", json={"name": "Shop"})
    png = write_image(tmp_path / "a.png", 1).read_bytes()
    result = client.post("/api/projects/shop/media/product-folders",
                         files=[("files", ("../../../../evil/x/a.png", io.BytesIO(png), "image/png"))])
    assert result.status_code == 200
    assert not (workspace.parent / "evil").exists() and not (workspace / "evil").exists()
    assert client.get("/api/projects/shop/file/../../../etc/passwd").status_code in (400, 404)
    assert client.get("/api/projects/shop/file/project.yaml").status_code == 404


def test_export_download_and_import_roundtrip(client):
    client.post("/api/projects", json={"name": "Bread"})
    archive = client.get("/api/projects/bread/export")
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
    imported = client.post("/api/projects/import", files={"file": ("bread.ivs.zip", archive.content, "application/zip")})
    assert imported.status_code == 200 and imported.json()["slug"] == "bread-2"


def test_source_test_and_activity_over_http(client, tmp_path):
    client.post("/api/projects", json={"name": "Bread"})
    clip = write_video(tmp_path / "clip.mp4")
    src = client.post("/api/projects/bread/sources", json={"kind": "video", "name": "Clip", "path": str(clip)}).json()
    tested = client.post(f"/api/projects/bread/sources/{src['id']}/test").json()
    assert tested["ok"] and client.get(f"/api/projects/bread/file/{tested['preview']}").status_code == 200
    job = client.post(f"/api/projects/bread/sources/{src['id']}/record", json={"seconds": 1}).json()
    from studio import jobs
    done = jobs.manager.wait(job["id"], 60)
    assert done.state == "done", done.error
    assert done.result["width"] == 320 and done.result["frames"] >= 15
    steps = [e["step"] for e in client.get("/api/projects/bread/activity").json()]
    assert steps[:3] == ["ingest.video", "source.record", "source.test"]
