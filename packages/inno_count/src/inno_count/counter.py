"""Line counting, per-track crops and vote, size at the line, health per minute (prompt §7.1, §7.3).

Rules this module enforces:
- One count per track, sent at the crossing, never waiting on type.
- A track counts when it was SEEN on the "before" side (any distance, so an object first seen near
  the line still counts: the legacy "first seen near the line is never counted" fault) and is then
  seen at least `hysteresis_px` past the line.
- Every time limit is in seconds of stream time, never in frames.
- A truncated box (touching the ROI or frame edge) keeps the track alive but is never used for size,
  crops or type.
- The classification follows with the same uid after `classify_timeout_s`; if nothing usable was
  collected the type is "unknown" and the count stands.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from . import __version__
from .geometry import Area, Line, PlaneCalibration, iou_matrix, is_truncated
from .types import ClassificationEvent, CountEvent, Detection, HealthEvent

DIRECTIONS = ("positive", "negative", "both")


class Classifier(Protocol):
    """The Classification Engine: K crops + measured size -> (class id, confidence, verdict)."""

    def classify(self, crops: list[np.ndarray], size: dict[str, Any] | None) -> tuple[str, float, str]: ...


@dataclass
class CounterConfig:
    line: Line
    direction: str = "positive"
    hysteresis_px: float = 6.0
    roi: tuple[float, float, float, float] | None = None
    area: Area | None = None               # polygon counting area; wins over roi when both are set
    edge_margin_px: float = 3.0
    k_crops: int = 5
    classify_timeout_s: float = 2.0
    isolation_iou: float = 0.05
    size_window_s: float = 0.5
    blind_gap_s: float = 0.5
    track_forget_s: float = 3.0
    calibration: PlaneCalibration | None = None
    class_names: list[str] | None = None   # for the multi-class detector baseline vote
    source_id: str = "src"
    model_sha: str = ""

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        if self.area is None and self.roi is not None:
            self.area = Area.from_rect(self.roi)
        if self.k_crops < 1 or self.classify_timeout_s <= 0:
            raise ValueError("k_crops >= 1 and classify_timeout_s > 0 are required.")


@dataclass
class _Obs:
    ts: float
    det: Detection
    truncated: bool
    d: float


@dataclass
class _TrackState:
    first_ts: float
    last_ts: float
    obs: deque = field(default_factory=lambda: deque(maxlen=400))
    # (ts, d, x, y) samples used to interpolate the crossing time and place
    seen_neg: tuple[float, float, float, float] | None = None   # last sample with d < 0
    seen_pos: tuple[float, float, float, float] | None = None   # last sample with d > 0
    first_after_neg: tuple[float, float, float, float] | None = None
    first_after_pos: tuple[float, float, float, float] | None = None
    uid: str | None = None
    crossing_ts: float | None = None
    classified: bool = False
    crops: list[tuple[float, np.ndarray | None]] = field(default_factory=list)
    votes: list[int] = field(default_factory=list)


class LineCounter:
    def __init__(self, config: CounterConfig, classifier: Classifier | None = None):
        self.cfg = config
        self.classifier = classifier
        self.tracks: dict[int, _TrackState] = {}
        self.camera_moved = False
        self.counted = 0
        # health
        self._last_frame_ts: float | None = None
        self._minute: int | None = None
        self._frames = 0
        self._blind = 0.0
        self._reconnects = 0
        self._moved_in_minute = False
        self._pending_blind: dict[int, float] = {}

    # ---------------------------------------------------------------- health

    def note_reconnect(self) -> None:
        self._reconnects += 1

    def _add_blind(self, t0: float, t1: float) -> None:
        """Split a gap [t0, t1] over the minutes it covers."""
        t = t0
        while t < t1:
            m = int(t // 60)
            end = min(t1, (m + 1) * 60)
            self._pending_blind[m] = self._pending_blind.get(m, 0.0) + (end - t)
            t = end

    def _health(self, ts: float) -> list[HealthEvent]:
        out = []
        minute = int(ts // 60)
        if self._minute is None:
            self._minute = minute
        while self._minute < minute:
            m = self._minute
            out.append(HealthEvent(m * 60.0, self._frames, round(self._blind + self._pending_blind.pop(m, 0.0), 3),
                                   self._reconnects, self._moved_in_minute or self.camera_moved))
            self._frames, self._blind, self._reconnects, self._moved_in_minute = 0, 0.0, 0, False
            self._minute = m + 1
        return out

    # ---------------------------------------------------------------- main step

    def update(self, ts: float, assignments: list[tuple[int, int]], detections: list[Detection],
               frame: np.ndarray | None = None, frame_size: tuple[int, int] | None = None) -> list[Any]:
        """Feed one processed frame. Returns the events produced by it, in order."""
        events: list[Any] = []
        if self._last_frame_ts is not None:
            gap = ts - self._last_frame_ts
            if gap > self.cfg.blind_gap_s:
                self._add_blind(self._last_frame_ts, ts)
        self._last_frame_ts = ts
        events += self._health(ts)
        self._frames += 1
        self._moved_in_minute |= self.camera_moved

        if frame is not None:
            frame_size = (frame.shape[1], frame.shape[0])
        bounds = self.cfg.roi or ((0, 0, frame_size[0], frame_size[1]) if frame_size else None)

        boxes = np.array([d.as_xyxy() for d in detections], dtype=np.float64).reshape(-1, 4)
        overlap = iou_matrix(boxes, boxes)
        np.fill_diagonal(overlap, 0.0)

        for track_id, di in assignments:
            det = detections[di]
            if self.cfg.area is not None:
                truncated = self.cfg.area.cuts(det, self.cfg.edge_margin_px) or bool(
                    frame_size and is_truncated(det, (0, 0, frame_size[0], frame_size[1]), self.cfg.edge_margin_px))
            else:
                truncated = bool(bounds and is_truncated(det, bounds, self.cfg.edge_margin_px))
            isolated = overlap.shape[0] == 0 or float(overlap[di].max(initial=0.0)) < self.cfg.isolation_iou
            cx, cy = det.center
            d = self.cfg.line.signed_distance(cx, cy)
            st = self.tracks.get(track_id)
            if st is None:
                st = self.tracks[track_id] = _TrackState(first_ts=ts, last_ts=ts)
            st.last_ts = ts
            st.obs.append(_Obs(ts, det, truncated, d))
            st.votes.append(det.cls)
            if not truncated and isolated:
                self._add_crop(st, ts, det, frame)
            ev = self._crossing(track_id, st, (ts, d, cx, cy))
            if ev:
                events.append(ev)

        events += self._due_classifications(ts)
        for tid in [t for t, st in self.tracks.items()
                    if ts - st.last_ts > self.cfg.track_forget_s and (st.uid is None or st.classified)]:
            del self.tracks[tid]
        return events

    def finish(self, ts: float) -> list[Any]:
        """End of stream: classify everything still pending and close the health minute."""
        events = self._due_classifications(ts, force=True)
        if self._minute is not None:
            events.append(HealthEvent(self._minute * 60.0, self._frames,
                                      round(self._blind + self._pending_blind.pop(self._minute, 0.0), 3),
                                      self._reconnects, self._moved_in_minute or self.camera_moved))
            self._minute = None
        return events

    # ---------------------------------------------------------------- crossing

    def _crossing(self, track_id: int, st: _TrackState, sample: tuple[float, float, float, float]) -> CountEvent | None:
        d = sample[1]
        if d < 0:
            st.seen_neg, st.first_after_neg = sample, None
        elif st.seen_neg and st.first_after_neg is None:
            st.first_after_neg = sample
        if d > 0:
            st.seen_pos, st.first_after_pos = sample, None
        elif st.seen_pos and st.first_after_pos is None:
            st.first_after_pos = sample

        if st.uid is not None:
            return None
        h = self.cfg.hysteresis_px
        crossed = None
        if self.cfg.direction in ("positive", "both") and st.seen_neg and d >= h:
            crossed = (st.seen_neg, st.first_after_neg or sample)
        elif self.cfg.direction in ("negative", "both") and st.seen_pos and d <= -h:
            crossed = (st.seen_pos, st.first_after_pos or sample)
        if crossed is None:
            return None
        (t0, d0, x0, y0), (t1, d1, x1, y1) = crossed
        f = (0 - d0) / (d1 - d0) if d1 != d0 else 1.0
        st.crossing_ts = round(t0 + f * (t1 - t0), 4)
        st.uid = f"{self.cfg.source_id}:{track_id}:{int(round(st.crossing_ts * 1000))}"
        self.counted += 1
        return CountEvent(st.uid, st.crossing_ts, track_id, self.cfg.model_sha, __version__,
                          x=round(x0 + f * (x1 - x0), 1), y=round(y0 + f * (y1 - y0), 1))

    # ---------------------------------------------------------------- crops, size, type

    def _add_crop(self, st: _TrackState, ts: float, det: Detection, frame: np.ndarray | None) -> None:
        crop = None
        if frame is not None:
            x1, y1 = max(int(det.x1), 0), max(int(det.y1), 0)
            x2, y2 = min(int(math.ceil(det.x2)), frame.shape[1]), min(int(math.ceil(det.y2)), frame.shape[0])
            if x2 > x1 and y2 > y1:
                crop = frame[y1:y2, x1:x2].copy()
        st.crops.append((ts, crop))
        if len(st.crops) > 4 * self.cfg.k_crops:
            st.crops = st.crops[::2]  # keep the spread along the track, bound the memory

    def _pick_crops(self, st: _TrackState) -> list[np.ndarray | None]:
        k = self.cfg.k_crops
        if len(st.crops) <= k:
            return [c for _, c in st.crops]
        idx = np.linspace(0, len(st.crops) - 1, k).round().astype(int)
        return [st.crops[i][1] for i in idx]

    def size_at_line(self, st: _TrackState) -> dict[str, Any] | None:
        """Median size over the frames around the crossing; truncated boxes excluded."""
        if st.crossing_ts is None:
            return None
        near = [o for o in st.obs if not o.truncated and abs(o.ts - st.crossing_ts) <= self.cfg.size_window_s]
        if not near:
            return None
        out: dict[str, Any] = {
            "w_px": round(float(np.median([o.det.w for o in near])), 2),
            "h_px": round(float(np.median([o.det.h for o in near])), 2),
            "n": len(near),
            "unit": "px",
        }
        if self.cfg.calibration:
            sizes = np.array([self.cfg.calibration.box_size_cm(o.det) for o in near])
            out.update(w_cm=round(float(np.median(sizes[:, 0])), 2), h_cm=round(float(np.median(sizes[:, 1])), 2),
                       unit="cm")
        return out

    def _due_classifications(self, ts: float, force: bool = False) -> list[ClassificationEvent]:
        out = []
        for st in self.tracks.values():
            if st.uid is None or st.classified:
                continue
            if not force and ts < st.crossing_ts + self.cfg.classify_timeout_s:
                continue
            st.classified = True
            crops = self._pick_crops(st)
            size = self.size_at_line(st)
            usable = [c for c in crops if c is not None]
            if self.classifier is not None and usable:
                cls, conf, verdict = self.classifier.classify(usable, size)
            elif self.cfg.class_names and st.votes:
                values, counts = np.unique(st.votes, return_counts=True)
                best = int(values[counts.argmax()])
                cls = self.cfg.class_names[best] if best < len(self.cfg.class_names) else "unknown"
                conf, verdict = float(counts.max() / counts.sum()), "whole"
            else:
                cls, conf, verdict = "unknown", 0.0, "unknown"
            out.append(ClassificationEvent(st.uid, round(ts, 4), cls, round(float(conf), 4), verdict, size,
                                           n_crops=len(crops), crops=usable))
        return out
