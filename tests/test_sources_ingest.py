from __future__ import annotations

import json

import pytest

from studio import db, ingest, projects, sources
from studio.explain import activity

from .conftest import write_image, write_video


@pytest.fixture
def slug(workspace):
    return projects.create_project("Bread")["slug"]


# ------------------------------------------------------------------ sources


def test_video_source_passes_test_connection_with_preview(slug, video):
    src = sources.add_source(slug, "video", "Old clip", path=str(video))
    result = sources.test_connection(slug, src["id"])
    assert result["ok"] and (result["width"], result["height"]) == (320, 240)
    assert result["fps"] == 20.0
    assert (projects.project_dir(slug) / result["preview"]).is_file()


def test_unreachable_camera_fails_in_plain_words(slug):
    # 192.0.2.0/24 is reserved for documentation: nothing answers there.
    src = sources.add_source(slug, "ip", "Ghost", url="rtsp://192.0.2.1:554/stream")
    result = sources.test_connection(slug, src["id"])
    assert result["ok"] is False
    assert "unreachable" in result["error_en"].lower() and result["error_ar"]


def test_source_validation(slug, tmp_path):
    with pytest.raises(sources.SourceError):
        sources.add_source(slug, "ip", "x", url="192.168.1.5")  # no scheme
    with pytest.raises(sources.SourceError):
        sources.add_source(slug, "video", "x", path=str(tmp_path / "missing.mp4"))
    with pytest.raises(sources.SourceError):
        sources.add_source(slug, "usb", "x")
    with pytest.raises(sources.SourceError):
        sources.add_source(slug, "telepathy", "x")


def test_record_then_ingest(slug, video):
    """A file source stands in for a live camera: record a clip, get per-frame timestamps, ingest it."""
    src = sources.add_source(slug, "video", "Stand-in camera", path=str(video))
    clip = sources.record(slug, src["id"], seconds=1.0)
    stamps = json.loads(clip.with_suffix(".timestamps.json").read_text())["seconds"]
    assert 15 <= len(stamps) <= 21 and stamps == sorted(stamps)
    media = ingest.add_video(slug, clip, source_id=src["id"])
    assert media["rel_path"].startswith("media/videos/rec_") and media["linked_path"] is None


# ------------------------------------------------------------------ video ingest


def test_link_leaves_the_video_in_place_and_copy_brings_it_in(slug, video, tmp_path):
    linked = ingest.add_video(slug, video, mode="link")
    assert linked["linked_path"] == str(video.resolve()) and linked["rel_path"] is None
    assert (linked["width"], linked["height"], linked["fps"], linked["frames"]) == (320, 240, 20.0, 30)
    assert linked["duration_s"] == 1.5

    other = write_video(tmp_path / "other.mp4", frames=10)
    copied = ingest.add_video(slug, other, mode="copy")
    assert copied["rel_path"] == "media/videos/other.mp4"
    assert (projects.project_dir(slug) / copied["rel_path"]).is_file()


def test_same_video_twice_is_refused(slug, video):
    ingest.add_video(slug, video)
    with pytest.raises(ingest.IngestError, match="already"):
        ingest.add_video(slug, video, mode="copy")


def test_first_video_of_an_angle_stores_a_reference_frame(slug, video):
    ingest.add_video(slug, video, angle_id="belt-top")
    angle = projects.project_dir(slug) / "sources" / "angles" / "belt-top"
    assert (angle / "reference.jpg").stat().st_size > 0
    assert json.loads((angle / "angle.json").read_text())["width"] == 320


def test_angles_are_never_mixed(slug, video, tmp_path):
    ingest.add_video(slug, video, angle_id="angle-1")
    wide = write_video(tmp_path / "wide.mp4", size=(640, 360))
    with pytest.raises(ingest.IngestError, match="new angle id"):
        ingest.add_video(slug, wide, angle_id="angle-1")
    assert ingest.add_video(slug, wide, angle_id="angle-2")["width"] == 640


def test_broken_video_is_refused(slug, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is not a video")
    with pytest.raises(ingest.IngestError):
        ingest.add_video(slug, fake)


# ------------------------------------------------------------------ pictures


def test_folder_per_product_makes_classes_with_human_labels(slug, product_root):
    result = ingest.add_product_folders(slug, product_root)
    classes = projects.get_project(slug)["classes"]
    by_name = {c["display_name"]: c["id"] for c in classes}
    assert set(by_name) == {"cans", "bottles", "سياحي كبير"}
    assert all(cid.isascii() for cid in by_name.values()), "class ids are ASCII; the Arabic name is the display name"
    assert result["added"] == 9

    with db.connect(projects.project_dir(slug)) as con:
        labels = con.execute("SELECT class_id, bbox, provenance FROM labels").fetchall()
    assert len(labels) == 9
    assert {row["provenance"] for row in labels} == {"human:tester"}
    assert all(row["bbox"] is None for row in labels), "sorting into folders labels the class, not a box"
    counts = {v["display_name"]: v["pictures"] for v in result["classes"].values()}
    assert counts == {"cans": 3, "bottles": 2, "سياحي كبير": 4}


def test_folder_per_product_twice_adds_nothing_new(slug, product_root):
    ingest.add_product_folders(slug, product_root)
    again = ingest.add_product_folders(slug, product_root)
    assert again["added"] == 0 and again["duplicate"] == 9
    stats = projects.store.stats(projects.project_dir(slug))
    assert (stats["images"], stats["labels_human"]) == (9, 9)
    assert len(projects.get_project(slug)["classes"]) == 3


def test_folder_without_products_is_explained(slug, tmp_path):
    flat = tmp_path / "flat"
    write_image(flat / "a.png", 1)
    with pytest.raises(ingest.IngestError, match="one sub-folder per product"):
        ingest.add_product_folders(slug, flat)


def test_loose_pictures_have_no_label_and_bad_files_are_counted(slug, tmp_path):
    folder = tmp_path / "loose"
    write_image(folder / "a.png", 1)
    write_image(folder / "b.png", 2)
    (folder / "broken.jpg").write_bytes(b"not a picture")
    (folder / "notes.txt").write_text("ignored")
    counts = ingest.add_images(slug, [folder])
    assert counts == {"added": 2, "duplicate": 0, "unreadable": 1}
    stats = projects.store.stats(projects.project_dir(slug))
    assert (stats["images"], stats["labels_human"], stats["labels_unreviewed"]) == (2, 0, 0)


# ------------------------------------------------------------------ explanation


def test_every_step_leaves_an_explained_log_entry(slug, video, product_root):
    src = sources.add_source(slug, "video", "Clip", path=str(video))
    sources.test_connection(slug, src["id"])
    ingest.add_video(slug, video)
    ingest.add_product_folders(slug, product_root)
    entries = activity.read(projects.project_dir(slug))
    steps = [e["step"] for e in reversed(entries)]
    assert steps == ["project.create", "source.add", "source.test", "ingest.video", "ingest.product_folders"]
    for entry in entries:
        assert entry["ar"] and entry["en"] and entry["user"] == "tester" and entry["ts"]
    video_entry = next(e for e in entries if e["step"] == "ingest.video")
    assert video_entry["numbers"]["frames"] == 30 and video_entry["why_ar"]


def test_a_recording_is_never_lost_to_an_angle_clash(slug, video, tmp_path):
    """The owner's case: an earlier 320×240 video holds angle-1; a camera then records at another
    resolution. The recording must be kept, under the camera's own angle."""
    ingest.add_video(slug, video, angle_id="angle-1")
    big = write_video(tmp_path / "cam.mp4", frames=40, size=(640, 360))
    src = sources.add_source(slug, "video", "Camera stand-in", path=str(big))
    media = sources.record_and_register(slug, src["id"], seconds=1.0)
    assert media["width"] == 640 and media["angle_id"] == f"{src['id'][4:]}-640x360"
    # even when the owner names the clashing angle, the clip is kept under the camera's angle
    again = sources.add_source(slug, "video", "Camera 2", path=str(write_video(tmp_path / "c2.mp4", frames=30, size=(640, 360))))
    kept = sources.record_and_register(slug, again["id"], seconds=0.5, angle_id="angle-1")
    assert kept["angle_id"].endswith("-640x360")


def test_recording_can_be_stopped_and_keeps_what_it_has(slug, tmp_path):
    long = write_video(tmp_path / "long.mp4", frames=200, size=(320, 240))
    src = sources.add_source(slug, "video", "Long", path=str(long))
    seen = []
    media = sources.record_and_register(slug, src["id"], seconds=60, progress=lambda f, m: seen.append(f),
                                        should_stop=lambda: len(seen) >= 3)
    assert 3 <= media["duration_s"] <= 5 and seen == sorted(seen)
