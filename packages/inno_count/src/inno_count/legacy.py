"""The old greedy centroid tracker, reproduced from the audit's description (audit §1 item 3).

Not the agent's code (that needs the ERP repository, for inno_count v0). Kept so the harness can
show it is able to fail: swapping ByteTrack for this must raise doubles on piled clips
(acceptance test 4). Its faults are kept on purpose:
- low-confidence boxes are thrown away before tracking;
- nearest-centroid greedy matching with a fixed pixel radius;
- tracks expire after a number of FRAMES, so behaviour changes with the processing rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import Detection


@dataclass
class _Track:
    id: int
    cx: float
    cy: float
    missed: int = 0


class GreedyCentroidTracker:
    name = "greedy"

    def __init__(self, conf: float = 0.25, max_dist_px: float = 60.0, max_missed_frames: int = 5):
        self.conf, self.max_dist, self.max_missed = conf, max_dist_px, max_missed_frames
        self.tracks: list[_Track] = []
        self._next_id = 1

    def reset(self) -> None:
        self.tracks, self._next_id = [], 1

    def update(self, ts: float, detections: list[Detection]) -> list[tuple[int, int]]:
        order = sorted((i for i, d in enumerate(detections) if d.score >= self.conf),
                       key=lambda i: -detections[i].score)
        free = {t.id: t for t in self.tracks}
        out = []
        for i in order:
            cx, cy = detections[i].center
            best, best_d = None, self.max_dist
            for t in free.values():
                d = math.hypot(t.cx - cx, t.cy - cy)
                if d < best_d:
                    best, best_d = t, d
            if best is None:
                best = _Track(self._next_id, cx, cy)
                self._next_id += 1
                self.tracks.append(best)
            else:
                del free[best.id]
            best.cx, best.cy, best.missed = cx, cy, 0
            out.append((best.id, i))
        for t in free.values():
            t.missed += 1
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed]
        return out
