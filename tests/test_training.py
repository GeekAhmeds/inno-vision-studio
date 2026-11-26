"""P4: dataset snapshots (human labels only, time-block splits) and the RF-DETR training runner."""

from __future__ import annotations

import json
import sys
import textwrap

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from inno_count.detector import OnnxDetector, decode_rfdetr
from studio import db, datasets, ingest, projects, registry, review, train, truth
from studio.api.app import create_app

from .conftest import make_rfdetr_onnx, write_video


def _project_with_frames(tmp_path, n=48, every_s=20.0):
    """n confirmed frames from one long (pretend) video, every_s apart, one box each but every 6th empty."""
    slug = projects.create_project("Bread train")["slug"]
    projects.add_class(slug, "loaf")
    video = ingest.add_video(slug, write_video(tmp_path / "v.mp4", frames=20))
    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(n):
        img = np.full((48, 64, 3), (i * 5) % 255, np.uint8)
        cv2.circle(img, (10 + i % 40, 24), 6, (255, 255, 255), -1)
        cv2.imwrite(str(folder / f"f{i:03d}.png"), img)
    ingest.add_images(slug, [folder])
    with db.connect(projects.project_dir(slug)) as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM media WHERE kind = 'image' ORDER BY origin_name")]
        for i, mid in enumerate(ids):
            con.execute("UPDATE media SET source_media_id = ?, source_t = ? WHERE id = ?", (video["id"], i * every_s, mid))
    for i, mid in enumerate(ids):
        boxes = [] if i % 6 == 0 else [{"x": 2 + i % 40, "y": 16, "w": 16, "h": 16, "class_id": "loaf"}]
        review.confirm(slug, mid, boxes)
    return slug, video, ids


def test_snapshot_uses_human_boxes_and_whole_time_blocks(tmp_path):
    slug, video, ids = _project_with_frames(tmp_path)
    # A machine proposal on a confirmed picture must never reach the dataset.
    with db.connect(projects.project_dir(slug)) as con:
        con.execute("INSERT INTO labels (media_id, class_id, bbox, provenance, created_at) VALUES (?, 'loaf', ?, 'auto:x', 'now')",
                    (ids[1], json.dumps([30, 30, 5, 5])))
    m = datasets.build_snapshot(slug, block_s=120)        # 6 frames per block -> 8 blocks
    d = datasets.snapshot_dir(slug, m["id"])
    total = sum(m["counts"][s]["images"] for s in datasets.SPLITS)
    assert total == len(ids) and all(m["counts"][s]["images"] > 0 for s in datasets.SPLITS)
    seen = {}
    for s in datasets.SPLITS:
        coco = json.loads((d / s / "_annotations.coco.json").read_text(encoding="utf-8"))
        assert coco["categories"] == [{"id": 0, "name": "loaf", "supercategory": "none"}]
        for im in coco["images"]:
            assert (d / s / im["file_name"]).exists()
            seen[im["id"]] = s
        assert all(a["bbox"] != [30, 30, 5, 5] for a in coco["annotations"])
    # Whole blocks: every frame of a block sits in the same split.
    for b in range(8):
        assert len({seen[mid] for mid in ids[b * 6:(b + 1) * 6]}) == 1
    assert m["counts"]["train"]["empty"] + m["counts"]["valid"]["empty"] + m["counts"]["test"]["empty"] == 8
    assert datasets.list_snapshots(slug)[0]["id"] == m["id"]


def test_snapshot_leaves_out_frames_near_truth_and_refuses_too_little(tmp_path):
    slug, video, ids = _project_with_frames(tmp_path, every_s=60.0)
    truth.create_clip(slug, video["id"], 0, 0.5, "normal")      # frames within the buffer of t=0 are left out
    m = datasets.build_snapshot(slug, block_s=120)
    assert m["left_out_near_truth"] > 0
    assert sum(m["counts"][s]["images"] for s in datasets.SPLITS) == len(ids) - m["left_out_near_truth"]
    with pytest.raises(datasets.DatasetError, match="time block"):
        datasets.build_snapshot(slug, block_s=100000)
    with pytest.raises(datasets.DatasetError):
        datasets.snapshot_dir(slug, "../etc")


def test_decode_rfdetr_scales_and_drops_background():
    dets = np.array([[0.5, 0.5, 0.2, 0.4], [0.1, 0.1, 0.1, 0.1]])
    logits = np.array([[4.0, -9.0], [-4.0, 9.0]])                 # 2nd query: only the background slot is high
    boxes, scores, classes = decode_rfdetr(dets, logits, 0.3, 1, 200, 100)
    assert len(boxes) == 1 and classes[0] == 0 and scores[0] > 0.9
    assert np.allclose(boxes[0], [80, 30, 120, 70])


def test_onnx_detector_reads_an_rfdetr_export(tmp_path):
    path = tmp_path / "m.onnx"
    make_rfdetr_onnx(path, np.array([[0.5, 0.5, 0.25, 0.5], [0.2, 0.2, 0.1, 0.1]]), np.array([[5.0, -5.0], [-5.0, -5.0]]))
    (tmp_path / "m.json").write_text(json.dumps({"names": ["loaf"], "conf": 0.4}), encoding="utf-8")
    det = OnnxDetector(path)
    assert det.format == "rfdetr"
    out = det.detect(np.zeros((100, 200, 3), np.uint8))
    assert len(out) == 1 and abs(out[0].x1 - 75) < 0.01 and abs(out[0].y2 - 75) < 0.01


FAKE_WORKER = textwrap.dedent('''
    import argparse, json, sys, pathlib
    sys.path.insert(0, {tests!r})
    import numpy as np
    from conftest import make_rfdetr_onnx
    ap = argparse.ArgumentParser()
    for k in ("--dataset", "--out", "--size", "--epochs", "--batch-size", "--grad-accum", "--device"):
        ap.add_argument(k)
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    assert (pathlib.Path(a.dataset) / "train" / "_annotations.coco.json").exists()
    (out / "lightning").mkdir(parents=True, exist_ok=True)
    rows = ["epoch,val/mAP_50_95"] + [f"{{e}},{{0.1 * (e + 1):.2f}}" for e in range(3)]
    (out / "lightning" / "metrics.csv").write_text("\\n".join(rows))
    for e in range(3):
        print(f"Epoch {{e}}: training...", flush=True)
    (out / "export").mkdir()
    make_rfdetr_onnx(out / "export" / "inference_model.onnx", np.array([[0.5, 0.5, 0.2, 0.2]]), np.array([[5.0, -5.0]]))
    print("RESULT " + json.dumps({{"onnx": str(out / "export" / "inference_model.onnx"), "resolution": 64}}))
''')


def test_training_registers_an_apache_model_with_its_snapshot(tmp_path):
    slug, _, _ = _project_with_frames(tmp_path)
    snap = datasets.build_snapshot(slug, block_s=120)["id"]
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER.format(tests=str(__import__("pathlib").Path(__file__).parent)), encoding="utf-8")
    seen = []
    res = train.run_training(slug, snap, epochs=3, worker=[sys.executable, str(script)],
                             progress=lambda f, m: seen.append(f))
    assert res["epochs_done"] == 3 and abs(res["val_map_50_95"] - 0.3) < 1e-9
    entry = registry.get_model(res["model"]["id"])
    assert entry["licence"] == "Apache-2.0" and snap in entry["note"] and entry["may_ship"]
    side = json.loads((projects.project_dir(slug) / "models" / res["run"] / "model.json").read_text(encoding="utf-8"))
    assert side["format"] == "rfdetr" and side["names"] == ["loaf"] and side["trained_on"] == snap
    assert OnnxDetector(entry["path"]).format == "rfdetr"


def test_training_failure_shows_the_log_tail(tmp_path):
    slug, _, _ = _project_with_frames(tmp_path)
    snap = datasets.build_snapshot(slug, block_s=120)["id"]
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nprint('CUDA out of memory')\nsys.exit(3)\n", encoding="utf-8")
    with pytest.raises(train.TrainError, match="out of memory"):
        train.run_training(slug, snap, worker=[sys.executable, str(bad)])
    with pytest.raises(train.TrainError, match="Size"):
        train.run_training(slug, snap, size="huge", worker=[sys.executable, str(bad)])


def test_training_api(tmp_path, monkeypatch):
    slug, _, _ = _project_with_frames(tmp_path)
    client = TestClient(create_app())
    info = client.get(f"/api/projects/{slug}/training").json()
    assert info["snapshots"] == [] and "train" in info and info["review"]["confirmed"] == 48
    job = client.post(f"/api/projects/{slug}/datasets", json={"block_s": 120}).json()
    for _ in range(200):
        j = client.get(f"/api/projects/{slug}/jobs/{job['id']}").json()
        if j["state"] in ("done", "failed"):
            break
        __import__("time").sleep(0.05)
    assert j["state"] == "done", j
    snap = client.get(f"/api/projects/{slug}/training").json()["snapshots"][0]["id"]
    monkeypatch.setattr(train, "available", lambda: {"installed": False, "how": "uv sync --extra train"})
    r = client.post(f"/api/projects/{slug}/train", json={"snapshot": snap})
    assert r.status_code == 400 and "not installed" in r.json()["detail"]
    assert client.post(f"/api/projects/{slug}/train", json={"snapshot": "ds-nope"}).status_code == 400
