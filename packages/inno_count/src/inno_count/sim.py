"""A synthetic belt with known truth, for tests and demos. Not used in production.

Objects of two sizes move down the frame through a horizontal counting line. Perspective makes the
same object bigger at the bottom (as on the real 2026-11-09 footage). "Piled" clips send objects in
overlapping clusters; the SimDetector then gives the covered object a low score or misses it, which
is exactly where a weak tracker breaks a track and double counts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .geometry import Line
from .types import Detection

BASE_SIZE = {0: (46.0, 36.0), 1: (64.0, 50.0)}   # class -> (w, h) at the top of the frame
COLOURS = {0: (90, 170, 215), 1: (40, 90, 140)}


@dataclass
class SimObject:
    id: int
    cls: int
    x: float          # centre x
    t0: float         # time its centre is at y0
    speed: float      # px/s, downward
    size_jitter: float

    def center_y(self, ts: float, y0: float) -> float:
        return y0 + self.speed * (ts - self.t0)


@dataclass
class SimClip:
    width: int = 640
    height: int = 360
    fps: float = 20.0
    duration_s: float = 60.0
    line_y: float = 216.0
    objects: list[SimObject] = field(default_factory=list)
    seed: int = 0
    background: np.ndarray | None = field(default=None, repr=False)

    @property
    def line(self) -> Line:
        # p1 on the right, p2 on the left: "positive" = moving down the frame.
        return Line(self.width, self.line_y, 0, self.line_y)

    @property
    def y0(self) -> float:
        return -40.0

    def scale(self, y: float) -> float:
        return 0.8 + 0.5 * max(0.0, min(1.0, y / self.height))

    def box(self, obj: SimObject, ts: float) -> tuple[float, float, float, float] | None:
        cy = obj.center_y(ts, self.y0)
        bw, bh = BASE_SIZE[obj.cls]
        s = self.scale(cy) * obj.size_jitter
        w, h = bw * s, bh * s
        x1, y1, x2, y2 = obj.x - w / 2, cy - h / 2, obj.x + w / 2, cy + h / 2
        cx1, cy1, cx2, cy2 = max(x1, 0), max(y1, 0), min(x2, self.width), min(y2, self.height)
        if cx2 - cx1 < 4 or cy2 - cy1 < 4:
            return None
        return cx1, cy1, cx2, cy2

    def visible(self, ts: float) -> list[tuple[SimObject, tuple[float, float, float, float]]]:
        out = []
        for obj in self.objects:
            b = self.box(obj, ts)
            if b is not None:
                out.append((obj, b))
        return out

    def crossings(self) -> list[tuple[float, int, float]]:
        """Truth: (time the centre crosses the line, class, x at the line) for every object."""
        out = [(o.t0 + (self.line_y - self.y0) / o.speed, o.cls, o.x) for o in self.objects]
        return sorted((round(t, 4), c, round(x, 1)) for t, c, x in out if 0 <= t <= self.duration_s)

    def _background(self) -> np.ndarray:
        if self.background is None:
            rng = np.random.default_rng(self.seed + 1000)
            bg = np.full((self.height, self.width, 3), 70, np.uint8)
            noise = rng.normal(0, 18, (self.height // 8, self.width // 8)).astype(np.float32)
            noise = cv2.resize(noise, (self.width, self.height), interpolation=cv2.INTER_CUBIC)
            bg = np.clip(bg.astype(np.float32) + noise[..., None], 0, 255).astype(np.uint8)
            for x in (40, self.width - 40):          # belt rails
                cv2.rectangle(bg, (x - 12, 0), (x + 12, self.height), (150, 150, 155), -1)
            for y in range(0, self.height, 45):       # bolts on the rails
                for x in (40, self.width - 40):
                    cv2.circle(bg, (x, y + 20), 5, (30, 30, 30), -1)
            cv2.rectangle(bg, (0, 0), (self.width, 14), (20, 20, 20), -1)
            self.background = bg
        return self.background

    def render(self, ts: float) -> np.ndarray:
        frame = self._background().copy()
        for obj, _ in self.visible(ts):
            cy = obj.center_y(ts, self.y0)
            bw, bh = BASE_SIZE[obj.cls]
            s = self.scale(cy) * obj.size_jitter
            cv2.ellipse(frame, (int(obj.x), int(cy)), (int(bw * s / 2), int(bh * s / 2)), 0, 0, 360,
                        COLOURS[obj.cls], -1, lineType=cv2.LINE_AA)
        return frame

    def frames(self, fps: float | None = None):
        fps = fps or self.fps
        n = int(self.duration_s * fps)
        for i in range(n):
            ts = round(i / fps, 6)
            yield ts, self.render(ts)

    def write_video(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.width, self.height))
        for _, frame in self.frames():
            writer.write(frame)
        writer.release()
        return path


def make_belt(seed: int = 0, duration_s: float = 60.0, rate_per_s: float = 0.8, piled: bool = False,
              fps: float = 20.0, speed: float = 80.0) -> SimClip:
    rng = np.random.default_rng(seed)
    clip = SimClip(fps=fps, duration_s=duration_s, seed=seed)
    t, oid = -3.0, 1
    lanes = np.linspace(130, clip.width - 130, 4)
    lane_free = np.full(len(lanes), -np.inf)
    gap_s = 1.6 * BASE_SIZE[1][1] / speed   # keep a lane clear long enough that loaves never touch
    while t < duration_s:
        t += float(rng.exponential(1.0 / rate_per_s))
        group = int(rng.integers(2, 4)) if piled else 1
        free = np.flatnonzero(lane_free <= t)
        if not piled and len(free) == 0:
            continue
        lane = int(rng.choice(free)) if not piled else int(rng.integers(0, len(lanes)))
        lane_free[lane] = t + gap_s
        base_x = float(lanes[lane]) + float(rng.normal(0, 8))
        for g in range(group):
            cls = int(rng.integers(0, 2))
            dx = dt = 0.0
            if piled and g:
                if rng.random() < 0.5:   # side by side, partly overlapping
                    dx = g * BASE_SIZE[1][0] * float(rng.uniform(0.55, 0.8)) * (1 if g % 2 else -1)
                    dt = g * float(rng.uniform(0.0, 0.2))
                else:                    # one behind the other, touching along the belt
                    dx = float(rng.normal(0, 8))
                    dt = g * float(rng.uniform(0.35, 0.55))
            clip.objects.append(SimObject(oid, cls, base_x + dx, t + dt, speed * float(rng.uniform(0.95, 1.05)),
                                          float(rng.uniform(0.95, 1.05))))
            oid += 1
    return clip


class SimDetector:
    """Detections from the clip's truth with jitter, misses and occlusion — deterministic per time."""

    def __init__(self, clip: SimClip, jitter_px: float = 2.0, miss_prob: float = 0.03, occluded_miss: float = 0.35,
                 merge_prob: float = 0.3, class_accuracy: float = 0.9, seed: int = 7):
        self.clip, self.jitter, self.miss_prob = clip, jitter_px, miss_prob
        self.occluded_miss, self.merge_prob = occluded_miss, merge_prob
        self.class_accuracy, self.seed = class_accuracy, seed

    def detect(self, frame: np.ndarray | None = None, ts: float | None = None) -> list[Detection]:
        if ts is None:
            raise ValueError("SimDetector needs the frame time.")
        key = hashlib.sha256(f"{self.seed}:{round(ts * 1000)}".encode()).digest()
        rng = np.random.default_rng(int.from_bytes(key[:8], "little"))
        vis = self.clip.visible(ts)
        out = []
        # Touching objects are sometimes seen as ONE box spanning both (audit §1 item 1).
        merged: set[int] = set()
        for i, (a, ba) in enumerate(vis):
            for b, bb in vis[i + 1:]:
                if a.id in merged or b.id in merged or max(_overlap(ba, bb), _overlap(bb, ba)) <= 0.15:
                    continue
                if rng.random() < self.merge_prob:
                    merged |= {a.id, b.id}
                    j = rng.normal(0, self.jitter, 4)
                    out.append(Detection(min(ba[0], bb[0]) + j[0], min(ba[1], bb[1]) + j[1],
                                         max(ba[2], bb[2]) + j[2], max(ba[3], bb[3]) + j[3],
                                         float(rng.uniform(0.5, 0.8)), max(a.cls, b.cls)))
        for obj, (x1, y1, x2, y2) in vis:
            if obj.id in merged:
                continue
            covered = any(other.id > obj.id and _overlap(b, (x1, y1, x2, y2)) > 0.15 for other, b in vis)
            if rng.random() < self.miss_prob or (covered and rng.random() < self.occluded_miss):
                continue
            score = float(rng.uniform(0.15, 0.4)) if covered else float(rng.uniform(0.75, 0.95))
            j = rng.normal(0, self.jitter, 4)
            cls = obj.cls if rng.random() < self.class_accuracy else 1 - obj.cls
            out.append(Detection(x1 + j[0], y1 + j[1], x2 + j[2], y2 + j[3], score, cls))
        return out


def _overlap(a, b) -> float:
    """Share of box a covered by box b."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ix * iy / area if area > 0 else 0.0
