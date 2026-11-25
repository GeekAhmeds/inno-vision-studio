"""Truth store, event matching, the harness end to end, and their HTTP endpoints."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from inno_count.sim import SimDetector, make_belt
from studio import ingest, projects, truth
from studio.api import create_app
from studio.explain import activity
from studio.harness.match import match, wilson
from studio.harness.run import HarnessError, run_clip, run_harness


# ------------------------------------------------------------------ matching

def test_match_is_one_to_one_within_the_window():
    m = match([1.0, 5.0, 9.0], [1.2, 1.3, 5.4, 20.0], window_s=1.0)
    assert m.matched == [(0, 0), (1, 2)] or m.matched == [(0, 1), (1, 2)]
    assert m.misses == [2] and len(m.extras) == 2
    s = m.summary()
    assert (s["misses"], s["extras"]) == (1, 2), "misses and extras are never netted"


def test_position_separates_a_double_from_a_miss_in_a_pile():
    # two loaves cross together at x=100 and x=300; the system counted the left one twice
    truth_t, truth_xy = [10.0, 10.2], [(100, 200), (300, 200)]
    ev_t, ev_xy = [10.05, 10.25], [(102, 200), (98, 200)]
    blind = match(truth_t, ev_t)
    assert (len(blind.misses), len(blind.extras)) == (0, 0), "time alone hides both errors"
    seen = match(truth_t, ev_t, truth_xy=truth_xy, event_xy=ev_xy)
    assert (len(seen.misses), len(seen.extras)) == (1, 1)


def test_wilson_interval():
    lo, hi = wilson(5, 100)
    assert lo < 0.05 < hi and 0.01 < lo and hi < 0.12
    assert wilson(0, 0) == (0.0, 1.0)


# ------------------------------------------------------------------ truth store

@pytest.fixture
def sim_project(workspace, tmp_path):
    clip = make_belt(seed=8, duration_s=30, piled=True)
    video = clip.write_video(tmp_path / "belt.mp4")
    slug = projects.create_project("Sim bread")["slug"]
    projects.add_class(slug, "small")
    projects.add_class(slug, "large")
    media = ingest.add_video(slug, video)
    projects.set_counting(slug, {"line": [clip.width, clip.line_y, 0, clip.line_y]})
    taps = [{"t": t, "class_id": ["small", "large"][k], "x": x, "y": clip.line_y} for t, k, x in clip.crossings()]
    return {"slug": slug, "clip": clip, "media": media, "taps": taps}


def _count(slug, clip_id, taps):
    c = truth.start_count(slug, clip_id)
    return truth.save_count(slug, c["id"], taps, finished=True)


def test_two_counts_are_needed_and_disagreement_blocks_the_clip(sim_project):
    """Acceptance 12."""
    slug, taps = sim_project["slug"], sim_project["taps"]
    clip = truth.create_clip(slug, sim_project["media"]["id"], 0, 30, "piled")
    assert clip["status"] == "needs_count_1"
    with pytest.raises(HarnessError, match="no final truth"):
        run_clip(slug, clip["id"], SimDetector(sim_project["clip"]))

    _count(slug, clip["id"], taps)
    second = [dict(t) for t in taps[1:]]                 # missed the first crossing
    second[3]["class_id"] = "large" if second[3]["class_id"] == "small" else "small"
    second.append({"t": 29.0, "class_id": "small", "x": 50, "y": 216})  # tapped something extra
    _count(slug, clip["id"], second)

    rec = truth.reconcile(slug, clip["id"])
    assert rec["status"] == "disagree"
    assert len(rec["only_in_a"]) == 1 and len(rec["only_in_b"]) == 1 and len(rec["class_conflicts"]) == 1
    assert truth.get_clip(slug, clip["id"])["status"] == "needs_reconcile"
    with pytest.raises(HarnessError):
        run_clip(slug, clip["id"], SimDetector(sim_project["clip"]))
    with pytest.raises(truth.TruthError, match="choose the type"):
        truth.resolve(slug, clip["id"], {})

    conflict = rec["class_conflicts"][0]
    final = truth.resolve(slug, clip["id"], {"keep_a": [0], "keep_b": [], "class": {str(conflict["a"]): conflict["class_a"]}})
    assert final["status"] == "final" and len(final["final_taps"]) == len(taps)
    assert [t["class_id"] for t in final["final_taps"]] == [t["class_id"] for t in taps]

    with pytest.raises(truth.TruthError):
        truth.start_count(slug, clip["id"])
    truth.lock_clip(slug, clip["id"])
    with pytest.raises(truth.TruthError, match="locked"):
        truth.delete_clip(slug, clip["id"])


def test_finished_count_cannot_change_and_taps_are_checked(sim_project):
    slug = sim_project["slug"]
    clip = truth.create_clip(slug, sim_project["media"]["id"], 5, 20, "normal")
    c = truth.start_count(slug, clip["id"])
    with pytest.raises(truth.TruthError, match="outside"):
        truth.save_count(slug, c["id"], [{"t": 25.0, "class_id": "small"}], False)
    with pytest.raises(truth.TruthError, match="Unknown class"):
        truth.save_count(slug, c["id"], [{"t": 6.0, "class_id": "pizza"}], False)
    truth.save_count(slug, c["id"], [{"t": 6.0, "class_id": "small"}], True)
    with pytest.raises(truth.TruthError, match="finished"):
        truth.save_count(slug, c["id"], [], True)


def test_clip_validation(sim_project):
    slug, mid = sim_project["slug"], sim_project["media"]["id"]
    with pytest.raises(truth.TruthError):
        truth.create_clip(slug, mid, 10, 5, "normal")
    with pytest.raises(truth.TruthError, match="only"):
        truth.create_clip(slug, mid, 0, 100, "normal")
    with pytest.raises(truth.TruthError):
        truth.create_clip(slug, mid, 0, 10, "sunny")


# ------------------------------------------------------------------ harness

def _final_clip(sim_project, condition="piled"):
    slug = sim_project["slug"]
    clip = truth.create_clip(slug, sim_project["media"]["id"], 0, 30, condition)
    _count(slug, clip["id"], sim_project["taps"])
    _count(slug, clip["id"], [{**t, "t": t["t"] + 0.08} for t in sim_project["taps"]])
    assert truth.reconcile(slug, clip["id"])["status"] == "agreed"
    truth.resolve(slug, clip["id"])
    return clip["id"]


def test_harness_end_to_end_reports_misses_and_extras_separately(sim_project):
    slug, sim = sim_project["slug"], sim_project["clip"]
    _final_clip(sim_project)
    det = SimDetector(sim)
    det.names = ["small", "large"]
    report = run_harness(slug, detector=det, fps_list=[20, 5])
    assert [r["fps"] for r in report["runs"]] == [20, 5]
    at20 = report["runs"][0]["overall"]
    assert at20["truth"] == len(sim_project["taps"])
    assert at20["truth"] - at20["misses"] + at20["extras"] == at20["events"]
    assert at20["miss_rate"] < 0.2 and at20["type_accuracy"] > 0.9 and at20["link_rate"] > 0.8
    assert at20["enough"] is False, "fewer than 300 crossings is not enough to judge"
    assert report["runs"][0]["conditions"]["piled"]["truth"] == at20["truth"]
    assert report["counting_core_version"]

    folder = projects.project_dir(slug)
    html = (folder / report["report_dir"] / "report.html").read_text(encoding="utf-8")
    assert "الفائت" in html and "data:image/jpeg;base64," in html
    saved = json.loads((folder / report["report_dir"] / "report.json").read_text(encoding="utf-8"))
    assert saved["runs"][0]["overall"]["misses"] == at20["misses"]
    entry = activity.read(folder)[0]
    assert entry["step"] == "harness.run" and entry["numbers"]["misses"] == at20["misses"]


def test_harness_is_deterministic(sim_project):
    slug, sim = sim_project["slug"], sim_project["clip"]
    clip_id = _final_clip(sim_project)
    a = run_clip(slug, clip_id, SimDetector(sim), fps=10)
    b = run_clip(slug, clip_id, SimDetector(sim), fps=10)
    assert a["events"] == b["events"] and a["summary"] == b["summary"]


def test_harness_needs_a_line_and_a_model(workspace, tmp_path):
    slug = projects.create_project("Empty")["slug"]
    with pytest.raises(HarnessError, match="model"):
        run_harness(slug)
    with pytest.raises(HarnessError, match="not found"):
        run_harness(slug, model_path=tmp_path / "none.onnx")


# ------------------------------------------------------------------ HTTP

def test_truth_flow_over_http(sim_project):
    slug = sim_project["slug"]
    client = TestClient(create_app())
    cfg = client.get(f"/api/projects/{slug}/counting").json()
    assert cfg["config"]["line"] == [640.0, 216.0, 0.0, 216.0] and cfg["angles"][0]["width"] == 640
    assert client.put(f"/api/projects/{slug}/counting", json={"line": [1, 1, 1, 1]}).status_code == 400

    frame = client.get(f"/api/projects/{slug}/media/{sim_project['media']['id']}/frame?t=5&width=320&mark_x=10&mark_y=10")
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"

    created = client.post(f"/api/projects/{slug}/truth", json={"media_id": sim_project["media"]["id"], "start_s": 0,
                                                                 "end_s": 10, "condition": "busy"}).json()
    cid = created["id"]
    for _ in range(100):  # the browser copy is built in the background
        state = client.get(f"/api/projects/{slug}/truth/{cid}").json()["proxy"]["state"]
        if state != "building":
            break
        time.sleep(0.2)
    assert state in ("ready", "failed")
    if state == "ready":
        stamps = client.get(f"/api/projects/{slug}/truth/{cid}/stamps").json()
        assert len(stamps) == 201 and stamps[0] == 0.0  # 0.00 … 10.00 s inclusive
        assert client.get(f"/api/projects/{slug}/truth/{cid}/proxy.webm").status_code == 200

    taps = [t for t in sim_project["taps"] if t["t"] <= 10]
    for shift in (0.0, 0.1):
        count = client.post(f"/api/projects/{slug}/truth/{cid}/counts").json()
        r = client.put(f"/api/projects/{slug}/truth-counts/{count['id']}",
                       json={"taps": [{**t, "t": t["t"] + shift} for t in taps], "finished": True})
        assert r.status_code == 200
    assert client.get(f"/api/projects/{slug}/truth/{cid}/reconcile").json()["status"] == "agreed"
    final = client.post(f"/api/projects/{slug}/truth/{cid}/resolve", json={}).json()
    assert final["status"] == "final" and len(final["final_taps"]) == len(taps)
    listing = client.get(f"/api/projects/{slug}/truth").json()
    assert listing["min_per_condition"] == 300 and listing["clips"][0]["status"] == "final"

    bad = client.post(f"/api/projects/{slug}/harness", json={"model_path": "/nowhere/model.onnx"})
    assert bad.status_code == 400 and "not found" in bad.json()["detail"]
    assert client.get(f"/api/projects/{slug}/reports").json() == []
    assert client.get(f"/api/projects/{slug}/reports/..%2Fetc/report.html").status_code == 404
