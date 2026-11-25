"""inno_count core: geometry, detector plumbing, tracker, counter, camera check, engine."""

from __future__ import annotations

import numpy as np
import pytest

from inno_count import __version__
from inno_count.bytetrack import ByteTrack
from inno_count.camera import CameraCheck
from inno_count.counter import CounterConfig, LineCounter
from inno_count.detector import ModelLoadError, OnnxDetector, decode
from inno_count.engine import Engine, run
from inno_count.geometry import Line, PlaneCalibration, letterbox, unletterbox
from inno_count.legacy import GreedyCentroidTracker
from inno_count.sim import SimDetector, make_belt
from inno_count.types import ClassificationEvent, CountEvent, Detection, HealthEvent

from studio.harness.match import match

from .conftest import make_constant_onnx

DOWN = Line(640, 200, 0, 200)  # horizontal line; "positive" = moving down the frame


def counts(events):
    return [e for e in events if isinstance(e, CountEvent)]


def sim_run(clip, tracker, fps, **engine_kw):
    engine = Engine(SimDetector(clip), tracker, LineCounter(CounterConfig(clip.line, class_names=["small", "large"])),
                    target_fps=fps, frame_size=(clip.width, clip.height), **engine_kw)
    frames = ((round(i / clip.fps, 6), None) for i in range(int(clip.duration_s * clip.fps)))
    return run(engine, frames), engine


def score(clip, events):
    truth = clip.crossings()
    c = counts(events)
    return match([t for t, _, _ in truth], [e.ts for e in c],
                 truth_xy=[(x, clip.line_y) for _, _, x in truth], event_xy=[(e.x, e.y) for e in c])


# ------------------------------------------------------------------ geometry

def test_letterbox_keeps_aspect_and_maps_back():
    image = np.zeros((360, 640, 3), np.uint8)
    out, info = letterbox(image, 320)
    assert out.shape == (320, 320, 3)
    assert info.scale == 0.5 and info.pad_x == 0 and info.pad_y == 70
    boxes = np.array([[10.0, 80.0, 50.0, 100.0]])  # in letterbox space
    assert np.allclose(unletterbox(boxes, info), [[20, 20, 100, 60]])


def test_line_side():
    assert DOWN.signed_distance(100, 150) < 0 < DOWN.signed_distance(100, 250)
    assert DOWN.signed_distance(5, 200) == 0


def test_plane_calibration_gives_centimetres():
    cal = PlaneCalibration([(0, 0), (200, 0), (200, 100), (0, 100)], width_cm=40, length_cm=20)
    w, h = cal.box_size_cm(Detection(50, 25, 100, 50, 0.9))
    assert w == pytest.approx(10, abs=1e-6) and h == pytest.approx(5, abs=1e-6)
    # perspective: the far (top) edge of the belt looks narrower in the image
    persp = PlaneCalibration([(80, 0), (120, 0), (200, 100), (0, 100)], width_cm=40, length_cm=20)
    near = persp.box_size_cm(Detection(90, 80, 110, 90, 0.9))
    far = persp.box_size_cm(Detection(95, 5, 105, 10, 0.9))
    assert far[0] > near[0] * 0.9, "the same pixel width is more centimetres far away"
    with pytest.raises(ValueError):
        PlaneCalibration([(0, 0), (1, 1)], 1, 1)


# ------------------------------------------------------------------ detector

def test_decode_both_layouts_and_nms():
    # (1, 4+nc, N): two overlapping boxes of class 1 and one of class 0
    raw = np.array([[[50, 52, 200], [50, 50, 200], [20, 20, 30], [20, 20, 30],
                     [0.1, 0.1, 0.8], [0.9, 0.7, 0.05]]], dtype=np.float32)
    boxes, scores, classes = decode(raw, conf=0.25, iou=0.5, nc=2)
    assert len(boxes) == 2 and set(classes.tolist()) == {0, 1}
    assert np.allclose(boxes[classes == 1][0], [40, 40, 60, 60])
    e2e = np.array([[[10, 10, 30, 30, 0.9, 2], [0, 0, 5, 5, 0.1, 0]]], dtype=np.float32)
    boxes, scores, classes = decode(e2e, conf=0.25, iou=0.5)
    assert boxes.tolist() == [[10, 10, 30, 30]] and classes.tolist() == [2]


def test_onnx_detector_letterboxes_and_maps_boxes_back(tmp_path):
    pytest.importorskip("onnxruntime")
    # one box at letterbox (cx 32, cy 32, w 16, h 16), class 0 score 0.9
    out = np.array([[[32.0], [32.0], [16.0], [16.0], [0.9]]], dtype=np.float32)
    model = tmp_path / "toy.onnx"
    make_constant_onnx(model, out, 64)
    (tmp_path / "toy.json").write_text('{"names": ["loaf"], "imgsz": 64, "conf": 0.25, "iou": 0.5}')
    det = OnnxDetector(model)
    assert det.names == ["loaf"] and len(det.sha256) == 64
    found = det.detect(np.zeros((64, 128, 3), np.uint8))  # 128×64 -> scale 0.5, pad 16 top
    assert len(found) == 1
    assert [round(v) for v in found[0].as_xyxy()] == [48, 16, 80, 48]


def test_model_that_cannot_load_is_refused_with_a_reason(tmp_path):
    with pytest.raises(ModelLoadError, match="not found"):
        OnnxDetector(tmp_path / "missing.onnx")
    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"not a model")
    with pytest.raises(ModelLoadError, match="could not be loaded"):
        OnnxDetector(broken)


# ------------------------------------------------------------------ counter

def _feed(counter, tracker, path, size=(640, 360), dt=0.05, start=0.0, w=40, h=30):
    """Move one box along `path` (list of centre points), one frame each."""
    events = []
    for i, (cx, cy) in enumerate(path):
        ts = round(start + i * dt, 4)
        det = [Detection(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, 0.9)] if cx is not None else []
        events += counter.update(ts, tracker.update(ts, det), det, None, size)
    return events


def test_first_seen_near_the_line_still_counts():
    """Legacy fault: a track first seen near the line was never counted."""
    c = LineCounter(CounterConfig(DOWN))
    ev = _feed(c, ByteTrack(), [(300, 198 + 3 * i) for i in range(10)])
    assert len(counts(ev)) == 1


def test_jitter_on_the_line_counts_once():
    c = LineCounter(CounterConfig(DOWN))
    path = [(300, 150 + 5 * i) for i in range(10)] + [(300, 200 + (8 if i % 2 else -8)) for i in range(20)]
    path += [(300, 210 + 5 * i) for i in range(10)]
    assert len(counts(_feed(c, ByteTrack(), path))) == 1


def test_direction():
    up = [(300, 300 - 5 * i) for i in range(40)]
    assert counts(_feed(LineCounter(CounterConfig(DOWN)), ByteTrack(), up)) == []
    assert len(counts(_feed(LineCounter(CounterConfig(DOWN, direction="both")), ByteTrack(), up))) == 1
    assert len(counts(_feed(LineCounter(CounterConfig(DOWN, direction="negative")), ByteTrack(), up))) == 1


def test_crossing_time_and_place_are_interpolated():
    c = LineCounter(CounterConfig(DOWN))
    ev = counts(_feed(c, ByteTrack(), [(300, 150 + 10 * i) for i in range(12)]))
    assert ev[0].ts == pytest.approx(0.25, abs=1e-6)      # y=200 halfway between frames 4 and 6
    assert (ev[0].x, ev[0].y) == (300, 200)
    assert ev[0].counting_core_version == __version__


def test_classification_follows_with_the_same_uid_and_never_blocks_the_count():
    c = LineCounter(CounterConfig(DOWN, classify_timeout_s=1.0))
    ev = _feed(c, ByteTrack(), [(300, 150 + 5 * i) for i in range(60)])
    count, = counts(ev)
    cls = [e for e in ev if isinstance(e, ClassificationEvent)]
    assert len(cls) == 1 and cls[0].uid == count.uid
    assert cls[0].ts >= count.ts + 1.0 and ev.index(count) < ev.index(cls[0])
    assert cls[0].cls == "unknown" and cls[0].verdict == "unknown"  # no classifier, no class names


def test_type_vote_and_classifier():
    names = ["small", "large"]
    c = LineCounter(CounterConfig(DOWN, class_names=names, classify_timeout_s=0.5))
    events = []
    t = ByteTrack()
    for i in range(40):
        ts = i * 0.05
        det = [Detection(280, 135 + 5 * i, 320, 165 + 5 * i, 0.9, cls=1 if i % 4 else 0)]
        events += c.update(ts, t.update(ts, det), det, None, (640, 360))
    events += c.finish(2.0)
    vote, = [e for e in events if isinstance(e, ClassificationEvent)]
    assert vote.cls == "large" and vote.class_confidence == pytest.approx(0.75, abs=0.05)

    class Fixed:
        def __init__(self):
            self.calls = []

        def classify(self, crops, size):
            self.calls.append((len(crops), size))
            return "whole_grain", 0.8, "whole"

    clf = Fixed()
    c = LineCounter(CounterConfig(DOWN, k_crops=5, classify_timeout_s=0.5), classifier=clf)
    frame = np.full((360, 640, 3), 90, np.uint8)
    t, events = ByteTrack(), []
    for i in range(40):
        ts = i * 0.05
        det = [Detection(280, 135 + 5 * i, 320, 165 + 5 * i, 0.9)]
        events += c.update(ts, t.update(ts, det), det, frame)
    result, = [e for e in events if isinstance(e, ClassificationEvent)]
    assert result.cls == "whole_grain" and result.n_crops == 5 and len(result.crops) == 5
    assert clf.calls[0][0] == 5 and clf.calls[0][1]["unit"] == "px"


def test_truncated_object_is_never_used_for_size_or_crops_and_counts_once():
    """Acceptance 10: a loaf cut by the frame edge is never counted twice and never used for size."""
    c = LineCounter(CounterConfig(DOWN, classify_timeout_s=0.5))
    frame = np.full((360, 640, 3), 90, np.uint8)
    t, events = ByteTrack(), []
    for i in range(60):  # half outside the left edge for its whole trip
        ts = i * 0.05
        det = [Detection(-1, 120 + 3 * i, 18, 150 + 3 * i, 0.8)]
        det[0].x1 = 0.0
        events += c.update(ts, t.update(ts, det), det, frame)
    events += c.finish(3.0)
    assert len(counts(events)) == 1
    result, = [e for e in events if isinstance(e, ClassificationEvent)]
    assert result.size_at_line is None and result.n_crops == 0


def test_size_at_line_uses_calibration():
    cal = PlaneCalibration([(0, 0), (640, 0), (640, 360), (0, 360)], width_cm=64, length_cm=36)
    c = LineCounter(CounterConfig(DOWN, calibration=cal, classify_timeout_s=0.5))
    ev = _feed(c, ByteTrack(), [(300, 150 + 5 * i) for i in range(40)], w=40, h=30)
    ev += c.finish(2.0)
    size = [e for e in ev if isinstance(e, ClassificationEvent)][0].size_at_line
    assert size["unit"] == "cm" and size["w_cm"] == pytest.approx(4.0, abs=0.1) and size["h_cm"] == pytest.approx(3.0, abs=0.1)


def test_health_per_minute_counts_frames_and_blind_seconds():
    c = LineCounter(CounterConfig(DOWN))
    t = ByteTrack()
    events = []
    for ts in [i * 0.1 for i in range(300)] + [50 + i * 0.1 for i in range(150)]:  # 30 s, gap, then 15 s
        events += c.update(ts, t.update(ts, []), [], None, (640, 360))
    c.note_reconnect()
    for ts in [65 + i * 0.5 for i in range(10)]:
        events += c.update(ts, t.update(ts, []), [], None, (640, 360))
    events += c.finish(70.0)
    health = [e for e in events if isinstance(e, HealthEvent)]
    assert [h.minute_start for h in health] == [0.0, 60.0]
    # minute 0: 300 frames, then 100 frames from 50.0 to 59.9; the 20.1 s gap is blind
    assert health[0].frames == 400 and health[0].blind_seconds == pytest.approx(20.1, abs=0.01)
    # minute 1: 50 frames to 64.9, then 10 at 2 fps after a reconnect (0.5 s gaps are not blind)
    assert health[1].reconnects == 1 and health[1].frames == 60 and health[1].blind_seconds == 0


# ------------------------------------------------------------------ tracker and engine on the simulator

def test_clean_belt_counts_exactly_at_any_rate():
    """Thresholds are in seconds: 20, 10 and 5 fps give the same answer on a clean belt."""
    clip = make_belt(seed=3, duration_s=60)
    results = {}
    for fps in (20, 10, 5):
        events, _ = sim_run(clip, ByteTrack(), fps)
        m = score(clip, events)
        results[fps] = (len(m.misses), len(m.extras))
    assert results[20] == (0, 0) and results[10] == (0, 0)
    assert results[5][0] <= 1 and results[5][1] == 0


def test_harness_can_fail_old_tracker_is_worse_on_piles():
    """Acceptance 4 (adapted, see docs/STATUS.md): swapping ByteTrack for the old greedy tracker makes
    the result measurably worse on piled clips at the low rates cheap hardware runs at."""
    for seed in (1, 2):
        clip = make_belt(seed=seed, duration_s=90, piled=True)
        new = score(clip, sim_run(clip, ByteTrack(), 5)[0])
        old = score(clip, sim_run(clip, GreedyCentroidTracker(), 5)[0])
        assert len(old.misses) + len(old.extras) > len(new.misses) + len(new.extras) + 10


def test_engine_rate_and_simulated_latency():
    clip = make_belt(seed=4, duration_s=20)
    _, full = sim_run(clip, ByteTrack(), None)
    _, five = sim_run(clip, ByteTrack(), 5)
    _, slow = sim_run(clip, ByteTrack(), None, simulated_latency_s=0.3)
    assert full.frames_processed == 400 and five.frames_processed == 100
    assert slow.frames_processed <= 400 / 6 + 1


def test_determinism_and_viewer_never_changes_the_count():
    """Acceptance 11: same clip twice -> identical events; a live viewer attached -> identical events."""
    clip = make_belt(seed=6, duration_s=20, piled=True)

    def full_run(viewers=()):
        engine = Engine(SimDetector(clip), ByteTrack(), LineCounter(CounterConfig(clip.line)), target_fps=10)
        return [e.to_dict() for e in run(engine, clip.frames(), viewers)]

    seen = []

    def viewer(ts, frame):  # a viewer that draws on what it gets and is slow
        frame[:] = 0
        seen.append(ts)

    a, b, c = full_run(), full_run(), full_run([viewer])
    assert a == b == c and len(seen) == 400


# ------------------------------------------------------------------ camera moved

def _scene(seed=0, w=640, h=360):
    return make_belt(seed=seed, duration_s=1).render(0.0)


def test_camera_moved_is_flagged_within_a_minute_and_day_night_is_not():
    """Acceptance 9."""
    ref = _scene()
    w = ref.shape[1]
    clock = np.zeros(ref.shape[:2], np.uint8)
    clock[0:14, :] = 1
    check = CameraCheck(ref, mask=clock)

    night = np.clip(ref.astype(np.float32) * 0.3 + np.random.default_rng(1).normal(0, 6, ref.shape), 0, 255).astype(np.uint8)
    ticking = ref.copy()
    ticking[0:14, 50:300] = 255  # the burned-in clock changes
    for ts in range(0, 120, 5):
        frame = night if ts % 2 else ticking
        assert check.check(float(ts), frame) is False, f"false alarm at {ts}s"

    shifted = np.roll(ref, int(0.05 * w), axis=1)
    moved_at = None
    for ts in range(120, 240, 1):
        if check.check(float(ts), shifted):
            moved_at = ts
            break
    assert moved_at is not None and moved_at - 120 <= 60
    assert check.last_shift_px == pytest.approx(0.05 * w, abs=3)
    assert check.check(300.0, ref) is True, "latched until a new reference is stored"

    covered = CameraCheck(ref)
    dark = np.full_like(ref, 12)
    assert covered.measure(dark)[0] is True


def test_counting_area_ignores_what_is_outside_it():
    """A hand or a stack outside the counting area never counts, even if it crosses the line."""
    clip = make_belt(seed=3, duration_s=30)
    roi = (0, 0, 320, 360)                       # left half only
    engine = Engine(SimDetector(clip), ByteTrack(), LineCounter(CounterConfig(clip.line, roi=roi)),
                    target_fps=10, frame_size=(clip.width, clip.height))
    events = run(engine, ((round(i / clip.fps, 6), None) for i in range(int(clip.duration_s * clip.fps))))
    got = counts(events)
    truth_left = [t for t, _, x in clip.crossings() if x < 320]
    assert got and all(e.x <= 320 for e in got)
    assert abs(len(got) - len(truth_left)) <= 1
