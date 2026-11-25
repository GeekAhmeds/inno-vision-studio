"""Detector + tracker + counter at a fixed processing rate on stream timestamps (prompt §7.3).

The rate is set here and nowhere else: whether a viewer is attached cannot change which frames are
processed (the legacy agent's rate changed ~7× with the live view). `simulated_latency_s` lets the
harness reproduce slow hardware: frames that arrive while the detector is still busy are dropped,
as they would be on a live stream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

import cv2
import numpy as np

from .camera import CameraCheck
from .counter import LineCounter
from .types import Detection


class Detector(Protocol):
    def detect(self, frame: np.ndarray, ts: float | None = None) -> list[Detection]: ...


class Tracker(Protocol):
    def update(self, ts: float, detections: list[Detection]) -> list[tuple[int, int]]: ...


class Engine:
    def __init__(self, detector: Detector, tracker: Tracker, counter: LineCounter, target_fps: float | None = None,
                 camera_check: CameraCheck | None = None, simulated_latency_s: float = 0.0,
                 frame_size: tuple[int, int] | None = None):
        if target_fps is not None and target_fps <= 0:
            raise ValueError("target_fps must be positive.")
        self.detector, self.tracker, self.counter = detector, tracker, counter
        self.period = 1.0 / target_fps if target_fps else 0.0
        self.camera_check = camera_check
        self.latency = simulated_latency_s
        self.frame_size = frame_size   # used only when frames carry no image (simulation)
        self._next_due: float | None = None
        self._busy_until = -1.0
        self.frames_seen = 0
        self.frames_processed = 0
        self.last_ts: float | None = None

    def process(self, ts: float, frame: np.ndarray | None) -> list[Any]:
        self.frames_seen += 1
        self.last_ts = ts
        if self.period:
            if self._next_due is not None and ts < self._next_due - 1e-6:
                return []
            due = (self._next_due if self._next_due is not None else ts) + self.period
            self._next_due = due if due > ts else ts + self.period
        if ts < self._busy_until - 1e-9:
            return []
        self._busy_until = ts + self.latency
        self.frames_processed += 1
        detections = self.detector.detect(frame, ts)
        roi = self.counter.cfg.roi
        if roi is not None:
            # Only what is on the belt counts: boxes whose centre is outside the counting area
            # (hands, stacks, the floor) never reach the tracker.
            x1, y1, x2, y2 = roi
            detections = [d for d in detections if x1 <= d.center[0] <= x2 and y1 <= d.center[1] <= y2]
        assignments = self.tracker.update(ts, detections)
        if self.camera_check is not None and frame is not None:
            self.counter.camera_moved = self.camera_check.check(ts, frame)
        return self.counter.update(ts, assignments, detections, frame, self.frame_size)

    def finish(self) -> list[Any]:
        return self.counter.finish(self.last_ts or 0.0)


def read_video(path: str | Path, start_s: float = 0.0, end_s: float | None = None) -> Iterator[tuple[float, np.ndarray]]:
    """Frames with their presentation time in seconds (never a frame index, prompt §5.2)."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise OSError(f"Could not open video: {path}")
    try:
        if start_s > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        index = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                return
            ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if ts <= 0 and index > 0 and fps > 0:  # some backends report 0 for every frame
                ts = index / fps
            index += 1
            if ts < start_s - 1e-6:
                continue
            if end_s is not None and ts > end_s:
                return
            yield ts, frame
    finally:
        cap.release()


def frame_at(path: str | Path, t: float) -> np.ndarray | None:
    """The frame shown at time t. Seeking alone lands on the previous keyframe for many codecs, so
    seek a little earlier and read forward to the exact time."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_MSEC, max(t - 2.0, 0.0) * 1000.0)
        last = None
        for _ in range(100000):
            ok, frame = cap.read()
            if not ok:
                return last
            pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if pos > t + 1e-3 and last is not None:
                return last
            last = frame
            if abs(pos - t) <= 1e-3:
                return frame
        return last
    finally:
        cap.release()


def run(engine: Engine, frames: Iterable[tuple[float, np.ndarray]],
        viewers: Iterable[Callable[[float, np.ndarray], None]] = ()) -> list[Any]:
    """Run a whole stream. Viewers get every frame but cannot affect what is counted."""
    viewers = list(viewers)
    events: list[Any] = []
    for ts, frame in frames:
        for view in viewers:
            view(ts, None if frame is None else frame.copy())
        events += engine.process(ts, frame)
    events += engine.finish()
    return events
