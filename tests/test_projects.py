from __future__ import annotations

import zipfile

import pytest

from studio import db, ingest, projects, sources
from studio.explain import activity


def test_create_makes_a_named_folder_with_its_own_store(workspace):
    meta = projects.create_project("Bread line", "bread", "count_per_type")
    folder = projects.project_dir(meta["slug"])
    assert meta["slug"] == "bread-line"
    for sub in ("sources", "media/videos", "datasets", "truth", "models", "reports"):
        assert (folder / sub).is_dir()
    assert (folder / "studio.db").exists()
    assert activity.read(folder)[0]["step"] == "project.create"


def test_arabic_name_gets_an_ascii_slug_and_keeps_its_name(workspace):
    meta = projects.create_project("عيش مخبز خبزة")
    assert meta["slug"].isascii() and meta["slug"].startswith("project-")
    assert projects.get_project(meta["slug"])["name"] == "عيش مخبز خبزة"


def test_two_projects_live_side_by_side(workspace, product_root):
    a = projects.create_project("Bread")
    b = projects.create_project("Canned goods")
    ingest.add_product_folders(b["slug"], product_root)
    assert projects.get_project(a["slug"])["classes"] == []
    assert len(projects.get_project(b["slug"])["classes"]) == 3
    assert {p["slug"] for p in projects.list_projects()} == {"bread", "canned-goods"}


def test_same_name_twice_gives_two_projects(workspace):
    assert projects.create_project("Bread")["slug"] == "bread"
    assert projects.create_project("Bread")["slug"] == "bread-2"


def test_rename_keeps_slug(workspace):
    meta = projects.create_project("Bread")
    projects.rename_project(meta["slug"], "خبز")
    assert projects.get_project("bread")["name"] == "خبز"


def test_duplicate_archive_delete(workspace, product_root):
    meta = projects.create_project("Bread")
    ingest.add_product_folders(meta["slug"], product_root)
    copy = projects.duplicate_project(meta["slug"], "Bread copy")
    assert copy["slug"] == "bread-copy"
    assert projects.store.stats(projects.project_dir("bread-copy"))["images"] == 9
    assert projects.archive_project("bread")["archived"] is True
    with pytest.raises(projects.ProjectError):
        projects.delete_project("bread", confirm="wrong")
    projects.delete_project("bread", confirm="bread")
    assert [p["slug"] for p in projects.list_projects()] == ["bread-copy"]


def test_bad_slug_cannot_escape_the_workspace(workspace):
    for bad in ("../x", "a/b", "", "..", "A B"):
        with pytest.raises(projects.ProjectError):
            projects.project_dir(bad)


def test_export_then_import_keeps_every_file_hash(workspace, product_root, video, tmp_path):
    meta = projects.create_project("Bread")
    slug = meta["slug"]
    ingest.add_product_folders(slug, product_root)
    ingest.add_video(slug, video, mode="copy")
    archive = projects.export_project(slug, tmp_path)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert {"project.yaml", "studio.db", "export_manifest.json", "activity.jsonl"} <= names
    assert all("\\" not in n for n in names), "archive paths must be posix so Windows and Linux agree"

    imported = projects.import_project(archive)
    assert imported["slug"] == "bread-2"
    before = projects.store.stats(projects.project_dir(slug))
    after = projects.store.stats(projects.project_dir("bread-2"))
    for key in ("videos", "images", "labels_human"):
        assert before[key] == after[key]
    with db.connect(projects.project_dir("bread-2")) as con:
        rows = con.execute("SELECT rel_path, sha256 FROM media").fetchall()
    assert len(rows) == 10
    for row in rows:  # the stored hash still matches the file that arrived
        assert ingest.media._sha256(projects.project_dir("bread-2") / row["rel_path"]) == row["sha256"]


def test_import_reports_linked_media_that_did_not_travel(workspace, video, tmp_path):
    slug = projects.create_project("Bread")["slug"]
    ingest.add_video(slug, video, mode="link")
    archive = projects.export_project(slug, tmp_path)
    video.unlink()
    imported = projects.import_project(archive)
    entry = activity.read(projects.project_dir(imported["slug"]))[0]
    assert entry["numbers"]["linked_media_missing"] == 1
    assert ingest.list_media(imported["slug"])[0]["missing"] is True


def test_import_refuses_a_damaged_or_foreign_archive(workspace, tmp_path):
    slug = projects.create_project("Bread")["slug"]
    archive = projects.export_project(slug, tmp_path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            dst.writestr(name, data + b"x" if name == "activity.jsonl" else data)
    with pytest.raises(projects.ProjectError, match="damaged"):
        projects.import_project(tampered)
    assert [p["slug"] for p in projects.list_projects()] == ["bread"], "a failed import leaves nothing behind"

    foreign = tmp_path / "foreign.zip"
    with zipfile.ZipFile(foreign, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(projects.ProjectError):
        projects.import_project(foreign)


def test_no_password_in_project_files_or_export(workspace, memory_keyring, tmp_path):
    slug = projects.create_project("Bread")["slug"]
    secret = "S3cret-Pass!word"
    src = sources.add_source(slug, "ip", "Belt cam", url=f"rtsp://admin:{secret}@192.168.1.50:554/stream1")
    assert src["params"]["url"] == "rtsp://admin@192.168.1.50:554/stream1"
    assert memory_keyring.get_password("inno-vision-studio", f"{slug}:{src['id']}") == secret

    archive = projects.export_project(slug, tmp_path)
    folder = projects.project_dir(slug)
    for path in [p for p in folder.rglob("*") if p.is_file()]:
        assert secret.encode() not in path.read_bytes(), f"password leaked into {path.name}"
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            assert secret.encode() not in zf.read(name), f"password leaked into export: {name}"

    sources.remove_source(slug, src["id"])
    assert memory_keyring.get_password("inno-vision-studio", f"{slug}:{src['id']}") is None
