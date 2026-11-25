"""P2: model registry, background jobs, frame sampling, pre-labelling, bake-off, review."""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from inno_count.sim import make_belt
from studio import db, ingest, jobs, prelabel, projects, registry, review, sample, truth
from studio.api import create_app

from .conftest import make_constant_onnx

# A toy detector on 640×360 frames, imgsz 64 (scale 0.1, 14 px padding top):
# box centre (32, 32) size 8×8 in letterbox space -> (280, 140)–(360, 220) in the frame.
GOOD = np.array([[[32.0], [32.0], [8.0], [8.0], [0.9]]], dtype=np.float32)
BAD = np.array([[[10.0], [20.0], [6.0], [6.0], [0.8]]], dtype=np.float32)   # somewhere else


@pytest.fixture
def toy_models(tmp_path, workspace):
    pytest.importorskip("onnxruntime")
    out = {}
    for name, arr, lic in (("good", GOOD, "Apache-2.0"), ("bad", BAD, "AGPL-3.0")):
        path = tmp_path / f"{name}.onnx"
        make_constant_onnx(path, arr, 64)
        (tmp_path / f"{name}.json").write_text(json.dumps({"names": ["bread"], "imgsz": 64, "conf": 0.25}))
        out[name] = registry.add_model(path, name, "detector", lic)
    return out


@pytest.fixture
def belt(workspace, tmp_path):
    clip = make_belt(seed=21, duration_s=120, rate_per_s=1.2)
    video = clip.write_video(tmp_path / "belt.mp4")
    slug = projects.create_project("Belt")["slug"]
    media = ingest.add_video(slug, video)
    return {"slug": slug, "media": media, "clip": clip}


# ------------------------------------------------------------------ registry

def test_registry_rules(tmp_path, toy_models):
    with pytest.raises(registry.RegistryError, match="not found"):
        registry.add_model(tmp_path / "x.onnx", "x", "detector", "MIT")
    (tmp_path / "w.pt").write_bytes(b"x")
    with pytest.raises(registry.RegistryError, match="ONNX"):
        registry.add_model(tmp_path / "w.pt", "w", "detector", "MIT")
    with pytest.raises(registry.RegistryError, match="licence"):
        registry.add_model(tmp_path / "good.onnx", "again", "detector", "")
    with pytest.raises(registry.RegistryError, match="already registered"):
        registry.add_model(tmp_path / "good.onnx", "again", "detector", "MIT")
    good, bad = toy_models["good"], toy_models["bad"]
    assert good["names"] == ["bread"] and good["may_ship"] and not good["copyleft"]
    assert bad["copyleft"] and not bad["may_ship"]
    assert registry.provenance(good).startswith("auto:good@") and len(registry.provenance(good)) == len("auto:good@") + 12


def test_copyleft_model_cannot_ship_under_permissive_policy(toy_models):
    """Acceptance 20."""
    with pytest.raises(registry.RegistryError, match="cannot ship"):
        registry.require_shippable(toy_models["bad"]["id"])
    assert registry.require_shippable(toy_models["good"]["id"])["name"] == "good"
    registry.set_policy("commercial_licence")
    assert registry.require_shippable(toy_models["bad"]["id"])["may_ship"] is True


# ------------------------------------------------------------------ jobs

def test_jobs_progress_errors_and_history(workspace):
    slug = projects.create_project("Jobs")["slug"]

    def ok(ctx):
        for i in range(3):
            ctx.progress(i / 3, f"step {i}")
        return {"n": 3}

    jobs.USER_ERRORS = (ValueError,)
    j1 = jobs.manager.submit(slug, "t", "تجربة", ok)
    j2 = jobs.manager.submit(slug, "t", "خطأ", lambda ctx: (_ for _ in ()).throw(ValueError("plain reason")))
    assert jobs.manager.wait(j1.id).state == "done" and jobs.manager.get(slug, j1.id)["result"] == {"n": 3}
    failed = jobs.manager.wait(j2.id)
    assert failed.state == "failed" and failed.error == "plain reason"
    assert {j["id"] for j in jobs.manager.list(slug)} == {j1.id, j2.id}
    on_disk = json.loads((projects.project_dir(slug) / "jobs" / f"{j1.id}.json").read_text())
    assert on_disk["state"] == "done"


def test_job_cancel(workspace):
    import threading

    slug = projects.create_project("Cancel")["slug"]
    gate = threading.Event()

    def slow(ctx):
        gate.wait(5)
        for _ in range(100):
            ctx.progress(0.5)
        return "never"

    j = jobs.manager.submit(slug, "t", "بطيء", slow)
    jobs.manager.cancel(j.id)
    gate.set()
    assert jobs.manager.wait(j.id).state == "cancelled"


# ------------------------------------------------------------------ sampling

def test_sampling_is_diverse_skips_truth_and_dedupes(belt, monkeypatch):
    slug, mid = belt["slug"], belt["media"]["id"]
    monkeypatch.setattr(sample, "TRUTH_BUFFER_S", 10.0)
    truth.create_clip(slug, mid, 40, 50, "normal")          # excluded: 30–60 s
    result = sample.run_sample(slug, n=20, every_s=1.0, negative_share=0.2)
    assert result["scanned"] > 60 and 1 <= result["diverse"] + result["low_motion"] <= 20
    with db.connect(projects.project_dir(slug)) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM media WHERE kind = 'image'")]
    assert rows and all(r["source_media_id"] == mid for r in rows)
    assert not [r for r in rows if 30 <= r["source_t"] <= 60], "no frame from a truth clip or its buffer"
    assert {r["sample_reason"] for r in rows} <= {"diverse", "low_motion"}
    assert all((projects.project_dir(slug) / r["rel_path"]).is_file() for r in rows)
    times = sorted(r["source_t"] for r in rows)
    assert times[0] < 30 and times[-1] > 60, "spread over the whole video"
    sample.run_sample(slug, n=20, every_s=1.0)
    with db.connect(projects.project_dir(slug)) as con:
        all_t = [r[0] for r in con.execute("SELECT source_t FROM media WHERE kind = 'image'")]
    assert len(all_t) == len(set(all_t)), "a second run never takes the same frame again"


def test_dhash_and_select_unit():
    rng = np.random.default_rng(0)
    base = (rng.random((36, 64)) * 255).astype(np.uint8)
    assert sample.hamming(sample.dhash(base), sample.dhash(base)) == 0
    cands = [sample.Candidate(1, float(t), rng.random((18, 32)), 100.0, float(t % 7), int(rng.integers(0, 2**63)))
             for t in range(100)]
    chosen = sample.select(cands, 10, negative_share=0.2)
    assert len(chosen) == 10 and sum(r == "low_motion" for _, r in chosen) == 2
    assert max(c.t for c, _ in chosen) - min(c.t for c, _ in chosen) > 70


# ------------------------------------------------------------------ prelabel, review, bake-off

def _frames(belt, n=6):
    sample.run_sample(belt["slug"], n=n, every_s=2.0, negative_share=0.0)
    with db.connect(projects.project_dir(belt["slug"])) as con:
        return [r["id"] for r in con.execute("SELECT id FROM media WHERE kind = 'image' ORDER BY id")]


def test_prelabel_review_and_bakeoff(belt, toy_models):
    slug = belt["slug"]
    ids = _frames(belt)
    good, bad = toy_models["good"], toy_models["bad"]
    res = prelabel.run_prelabel(slug, good["id"])
    assert res["images"] == len(ids) and res["boxes"] == len(ids)
    frame = review.get_frame(slug, ids[0])
    (prov, boxes), = frame["proposals"].items()
    assert prov == registry.provenance(good)
    assert boxes[0]["class_id"] == "object" and boxes[0]["x"] == pytest.approx(280, abs=1) and boxes[0]["w"] == pytest.approx(80, abs=1)
    assert frame["human"] == [] and frame["review"] is None

    st = review.stats(slug)
    assert st["confirmed"] == 0 and st["with_proposals"] == len(ids)
    assert set(review.queue(slug)) == set(ids)

    # The reviewer accepts the good box on every frame but the last, which is empty (a negative).
    for mid in ids[:-1]:
        b = review.get_frame(slug, mid)["proposals"][prov][0]
        review.confirm(slug, mid, [{**b, "class_id": "object"}])
    review.confirm(slug, ids[-1], [])
    st = review.stats(slug)
    assert st["confirmed"] == len(ids) and st["negatives"] == 1 and st["remaining"] == 0
    with db.connect(projects.project_dir(slug)) as con:
        human = con.execute("SELECT provenance FROM labels WHERE provenance LIKE 'human:%'").fetchall()
    assert len(human) == len(ids) - 1 and {r[0] for r in human} == {"human:tester"}
    assert review.get_frame(slug, ids[0])["proposals"], "proposals stay for the bake-off"

    with pytest.raises(review.ReviewError, match="Unknown class"):
        review.confirm(slug, ids[0], [{"x": 10, "y": 10, "w": 20, "h": 20, "class_id": "pizza"}])
    with pytest.raises(review.ReviewError, match="outside"):
        review.confirm(slug, ids[0], [{"x": 700, "y": 10, "w": 20, "h": 20}])

    result = prelabel.run_bakeoff(slug, [bad["id"], good["id"]])
    first, second = result["models"]
    # the good model finds every confirmed box, but also boxes the frame the reviewer marked empty
    assert first["name"] == "good" and first["recall"] == 1.0 and first["fp_on_empty_frames"] == 1
    assert first["precision"] == pytest.approx((len(ids) - 1) / len(ids), abs=1e-3)
    assert second["name"] == "bad" and second["precision"] == 0.0 and second["may_ship"] is False
    assert result["enough"] is False


# ------------------------------------------------------------------ HTTP

def test_labelling_over_http(belt, toy_models):
    slug = belt["slug"]
    client = TestClient(create_app())
    models = client.get("/api/models").json()
    assert models["policy"] == "permissive_only" and len(models["models"]) == 2
    assert client.post("/api/models", json={"path": "/nope.onnx", "name": "x", "licence": "MIT"}).status_code == 400

    job = client.post(f"/api/projects/{slug}/sample", json={"n": 5, "every_s": 2}).json()
    done = jobs.manager.wait(job["id"], 120)
    assert done.state == "done", done.error
    assert client.get(f"/api/projects/{slug}/jobs/{job['id']}").json()["result"]["scanned"] > 0
    assert client.post(f"/api/projects/{slug}/sample", json={"n": 0}).status_code == 400

    job = client.post(f"/api/projects/{slug}/prelabel", json={"model_id": toy_models["good"]["id"]}).json()
    assert jobs.manager.wait(job["id"], 120).state == "done"
    overview = client.get(f"/api/projects/{slug}/review").json()
    assert overview["stats"]["with_proposals"] == overview["stats"]["images"] > 0 and "object" in overview["classes"]
    first = overview["queue"][0]
    frame = client.get(f"/api/projects/{slug}/review/{first}").json()
    box = next(iter(frame["proposals"].values()))[0]
    saved = client.put(f"/api/projects/{slug}/review/{first}", json={"boxes": [box]}).json()
    assert saved["review"] == "confirmed" and len(saved["human"]) == 1
    assert client.post(f"/api/projects/{slug}/review/{first}/reopen").json()["review"] is None

    bad_job = client.post(f"/api/projects/{slug}/harness", json={"model_path": "/nowhere.onnx"})
    assert bad_job.status_code == 400
    assert any(j["kind"] == "sample" for j in client.get(f"/api/projects/{slug}/jobs").json())
