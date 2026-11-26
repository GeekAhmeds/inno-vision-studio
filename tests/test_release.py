"""P5: gate (paired, per condition, noise margin), publish inactive, shadow, activate, rollback."""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from inno_count.sim import SimDetector
from studio import gate, projects, publish, registry
from studio.api.app import create_app

from .conftest import make_constant_onnx
from .test_truth_harness import _final_clip, sim_project  # noqa: F401  (fixture)


# ------------------------------------------------------------------ the comparison, by itself

def _res(clip_id, cond, truth, miss_ids, extras):
    return {"clip_id": clip_id, "condition": cond, "miss_ids": list(miss_ids),
            "summary": {"truth": truth, "events": truth - len(miss_ids) + extras, "misses": len(miss_ids),
                        "extras": extras, "confusion": {}}}


S = {"z": 1.96, "min_crossings": 300, "min_type_accuracy": None}


def test_gate_refuses_better_total_but_worse_night_misses():
    """Acceptance 5, first half."""
    cur = [_res(1, "normal", 400, range(10), 60), _res(2, "night", 400, range(5), 5)]
    cand = [_res(1, "normal", 400, range(10), 5), _res(2, "night", 400, range(40), 5)]   # 55 fewer extras, 35 more night misses
    v = gate.compare(cand, cur, S)
    assert v["verdict"] == "fail" and any("night" in r and "misses" in r for r in v["reasons"])
    assert v["conditions"]["normal"]["extras_better"] and v["conditions"]["night"]["misses_worse"]


def test_gate_does_not_refuse_a_difference_inside_the_noise_margin():
    """Acceptance 5, second half: 2 more misses out of 400 is noise."""
    cur = [_res(1, "night", 400, range(10), 8)]
    cand = [_res(1, "night", 400, list(range(2, 12)) + [20, 21], 9)]
    v = gate.compare(cand, cur, S)
    assert v["verdict"] == "pass" and not v["clearly_better"]
    assert v["conditions"]["night"]["missed_by_candidate_only"] == 4 and v["conditions"]["night"]["missed_by_current_only"] == 2


def test_gate_ignores_small_conditions_and_says_when_there_is_too_little():
    cur = [_res(1, "night", 40, [], 0), _res(2, "normal", 400, range(5), 5)]
    cand = [_res(1, "night", 40, range(20), 0), _res(2, "normal", 400, range(5), 5)]
    v = gate.compare(cand, cur, S)
    assert v["verdict"] == "pass" and not v["conditions"]["night"]["used"]
    assert gate.compare(cand[:1], cur[:1], S)["verdict"] == "insufficient"
    with pytest.raises(gate.GateError):
        gate.compare(cand, cur[::-1], S)


def test_gate_type_threshold():
    r = _res(1, "normal", 400, [], 0)
    r["summary"]["confusion"] = {"a": {"a": 90, "b": 10}}
    v = gate.compare([r], None, {**S, "min_type_accuracy": 0.95})
    assert v["verdict"] == "fail" and "type accuracy" in v["reasons"][0] and v["first_model"]


# ------------------------------------------------------------------ end to end on the simulated belt

class _Drop:
    """A worse model: misses every second loaf."""

    def __init__(self, inner):
        self.inner, self.names = inner, getattr(inner, "names", None)

    def detect(self, frame, ts=None):
        return [d for d in self.inner.detect(frame, ts) if int(d.center[0]) % 2 == 0]


def _model(tmp_path, name, licence="Apache-2.0"):
    path = tmp_path / f"{name}.onnx"
    make_constant_onnx(path, np.zeros((1, 6, 4 + 2), np.float32) + sum(map(ord, name)), 32)
    (tmp_path / f"{name}.json").write_text(json.dumps({"names": ["small", "large"]}), encoding="utf-8")
    return registry.add_model(path, name, "detector", licence)


@pytest.fixture
def release(sim_project, tmp_path):
    slug, sim = sim_project["slug"], sim_project["clip"]
    _final_clip(sim_project)
    gate.set_settings(slug, {"min_crossings": 5, "fps": 10})
    old, new, bad = _model(tmp_path, "old"), _model(tmp_path, "new"), _model(tmp_path, "agpl", "AGPL-3.0")
    publish.adopt_current(slug, old["id"], "r2 at the bakery")
    det = SimDetector(sim)
    det.names = ["small", "large"]
    return {"slug": slug, "sim": sim, "old": old, "new": new, "agpl": bad, "det": det}


def test_gate_end_to_end_and_the_whole_release_flow(release):
    slug, det = release["slug"], release["det"]
    worse = gate.run_gate(slug, release["new"]["id"], detectors={"candidate": _Drop(det), "current": det})
    assert worse["verdict"] == "fail" and worse["current"]["id"] == release["old"]["id"]
    with pytest.raises(publish.PublishError, match="passed the gate"):
        publish.publish(slug, worse["report_dir"].split("/")[-1])

    same = gate.run_gate(slug, release["new"]["id"], detectors={"candidate": det, "current": det})
    assert same["verdict"] == "pass" and not same["clearly_better"]
    name = same["report_dir"].split("/")[-1]
    assert gate.list_reports(slug)[0]["name"] == name
    pending = publish.publish(slug, name)
    folder = projects.project_dir(slug) / pending["folder"]
    side = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    assert (folder / "model.onnx").exists() and (folder / "gate_report.html").exists()
    assert side["gate_report_sha"] == same["report_sha"] and side["model_sha256"] == release["new"]["sha256"]
    assert side["counting"]["line"] and side["class_map"] == {"small": "small", "large": "large"}
    assert side["counting_core_version"] and side["licence"] == "Apache-2.0"

    with pytest.raises(publish.PublishError, match="shadow"):
        publish.activate(slug)
    publish.add_shadow(slug, "2026-11-27", current_count=10210, candidate_count=10180, physical_count=10200)
    cur = publish.activate(slug)
    assert cur["sha256"] == release["new"]["sha256"]
    dep = publish.get_deployment(slug)
    assert dep["pending"] is None and dep["history"][-1]["sha256"] == release["old"]["sha256"]

    back = publish.rollback(slug, "night counts drifted")
    assert back["sha256"] == release["old"]["sha256"] and back["restored"]
    # The gate compared "new" with "old", and "old" is in use again: publishing it again is allowed.
    assert publish.publish(slug, name)["sha256"] == release["new"]["sha256"]
    with pytest.raises(publish.PublishError, match="already waiting"):
        publish.publish(slug, name)


def test_gate_refuses_copyleft_candidates_and_stale_reports(release):
    slug, det = release["slug"], release["det"]
    with pytest.raises(registry.RegistryError, match="permissive"):
        gate.run_gate(slug, release["agpl"]["id"], detectors={"candidate": det, "current": det})
    rep = gate.run_gate(slug, release["new"]["id"], detectors={"candidate": det, "current": det})
    dep = publish.get_deployment(slug)
    dep["current"]["sha256"] = "0" * 64           # somebody changed the model in use after the gate
    publish._save_deployment(slug, dep)
    with pytest.raises(publish.PublishError, match="out of date"):
        publish.publish(slug, rep["report_dir"].split("/")[-1])


def test_release_api(release):
    slug = release["slug"]
    client = TestClient(create_app())
    info = client.get(f"/api/projects/{slug}/release").json()
    assert info["deployment"]["current"]["name"] == "old" and info["settings"]["min_crossings"] == 5
    assert client.put(f"/api/projects/{slug}/gate-settings", json={"z": 9}).status_code == 400
    assert client.post(f"/api/projects/{slug}/gate", json={"candidate_id": release["agpl"]["id"]}).status_code == 400
    assert client.post(f"/api/projects/{slug}/activate", json={}).status_code == 400
    assert client.post(f"/api/projects/{slug}/shadow", json={"day": "bad", "current_count": 1, "candidate_count": 1}).status_code == 400
    assert client.post(f"/api/projects/{slug}/rollback", json={}).status_code == 400
