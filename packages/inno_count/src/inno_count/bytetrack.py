"""ByteTrack, written here so the core carries no AGPL code (prompt rule 9).

Algorithm from Zhang et al., "ByteTrack: Multi-Object Tracking by Associating Every Detection
Box" (ECCV 2022): a Kalman filter per track, then two association rounds — high-score boxes
against all tracks, then low-score boxes against the tracks still unmatched. The low-score round is
what keeps an occluded object (a loaf half under another) on its own track.

Differences from the paper's code, on purpose:
- every threshold is in seconds of stream time, and the Kalman step uses the real time between
  frames, so the result does not change when the processing rate changes (audit §1 item 3);
- a new track is tentative until its second hit and is dropped if that does not come;
- a new track may start from a box above `new_track` even if it is below `high` (see update).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .geometry import iou_matrix
from .types import Detection

TENTATIVE, TRACKED, LOST = "tentative", "tracked", "lost"


class _Kalman:
    """Constant-velocity filter on (cx, cy, w, h). Velocities in px/s. Noise scales with box height."""

    def __init__(self, box: np.ndarray):
        cx, cy, w, h = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1]
        self.x = np.array([cx, cy, w, h, 0, 0, 0, 0], dtype=np.float64)
        s = max(h, 1.0)
        self.P = np.diag([(0.1 * s) ** 2] * 4 + [(2.0 * s) ** 2] * 4)

    def predict(self, dt: float) -> None:
        if dt <= 0:
            return
        F = np.eye(8)
        F[:4, 4:] = np.eye(4) * dt
        s = max(self.x[3], 1.0)
        q_pos, q_vel = (0.05 * s) ** 2 * dt, (1.0 * s) ** 2 * dt
        Q = np.diag([q_pos] * 4 + [q_vel] * 4)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, box: np.ndarray) -> None:
        z = np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1]])
        Hm = np.zeros((4, 8))
        Hm[:, :4] = np.eye(4)
        s = max(self.x[3], 1.0)
        R = np.diag([(0.05 * s) ** 2] * 4)
        S = Hm @ self.P @ Hm.T + R
        K = self.P @ Hm.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - Hm @ self.x)
        self.P = (np.eye(8) - K @ Hm) @ self.P

    def box(self) -> np.ndarray:
        cx, cy, w, h = self.x[:4]
        w, h = max(w, 1.0), max(h, 1.0)
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


@dataclass
class _Track:
    id: int
    kf: _Kalman
    score: float
    last_ts: float
    hits: int = 1
    state: str = TENTATIVE
    predicted_ts: float = field(default=0.0)


class ByteTrack:
    name = "bytetrack"

    def __init__(self, high: float = 0.5, low: float = 0.1, new_track: float = 0.3,
                 match_iou: float = 0.2, low_match_iou: float = 0.5, lost_s: float = 1.0):
        if not 0 <= low < high <= 1:
            raise ValueError("Need 0 <= low < high <= 1.")
        self.high, self.low, self.new_track = high, low, new_track
        self.match_iou, self.low_match_iou, self.lost_s = match_iou, low_match_iou, lost_s
        self.tracks: list[_Track] = []
        self._next_id = 1

    def reset(self) -> None:
        self.tracks, self._next_id = [], 1

    @staticmethod
    def _assign(track_boxes: np.ndarray, det_boxes: np.ndarray, min_iou: float) -> list[tuple[int, int]]:
        if len(track_boxes) == 0 or len(det_boxes) == 0:
            return []
        iou = iou_matrix(track_boxes, det_boxes)
        rows, cols = linear_sum_assignment(-iou)
        return [(r, c) for r, c in zip(rows, cols) if iou[r, c] >= min_iou]

    def update(self, ts: float, detections: list[Detection]) -> list[tuple[int, int]]:
        """Returns (track_id, detection index) for every detection that belongs to a track now."""
        for t in self.tracks:
            t.kf.predict(ts - t.predicted_ts)
            t.predicted_ts = ts

        boxes = np.array([d.as_xyxy() for d in detections], dtype=np.float64).reshape(-1, 4)
        scores = np.array([d.score for d in detections], dtype=np.float64)
        high_idx = np.flatnonzero(scores >= self.high)
        low_idx = np.flatnonzero((scores >= self.low) & (scores < self.high))

        out: list[tuple[int, int]] = []
        matched_tracks: set[int] = set()

        # Round 1: high-score boxes against every live track (tracked, lost, tentative).
        pool = list(range(len(self.tracks)))
        pool_boxes = np.array([self.tracks[i].kf.box() for i in pool]).reshape(-1, 4)
        used_high: set[int] = set()
        for r, c in self._assign(pool_boxes, boxes[high_idx], self.match_iou):
            ti, di = pool[r], int(high_idx[c])
            self._hit(self.tracks[ti], boxes[di], scores[di], ts)
            matched_tracks.add(ti)
            used_high.add(di)
            out.append((self.tracks[ti].id, di))

        # Round 2: low-score boxes against tracks that were being followed (or just started) and
        # missed round 1.
        pool = [i for i, t in enumerate(self.tracks) if i not in matched_tracks and t.state != LOST]
        pool_boxes = np.array([self.tracks[i].kf.box() for i in pool]).reshape(-1, 4)
        used_low: set[int] = set()
        for r, c in self._assign(pool_boxes, boxes[low_idx], self.low_match_iou):
            ti, di = pool[r], int(low_idx[c])
            self._hit(self.tracks[ti], boxes[di], scores[di], ts)
            matched_tracks.add(ti)
            used_low.add(di)
            out.append((self.tracks[ti].id, di))

        # Unmatched tracks: tentative ones die, tracked ones become lost, lost ones expire.
        survivors = []
        for i, t in enumerate(self.tracks):
            if i not in matched_tracks:
                if t.state == TENTATIVE:
                    continue
                t.state = LOST
                if ts - t.last_ts > self.lost_s:
                    continue
            survivors.append(t)
        self.tracks = survivors

        # New tentative tracks from boxes nobody claimed. Unlike the paper, a low-score box may start
        # one: a loaf half under another can stay low-score for its whole trip and must still count.
        # A tentative track needs a second hit to survive, which filters one-frame clutter.
        for di in np.flatnonzero(scores >= self.new_track):
            di = int(di)
            if di in used_high or di in used_low:
                continue
            t = _Track(self._next_id, _Kalman(boxes[di]), float(scores[di]), ts, predicted_ts=ts)
            self._next_id += 1
            self.tracks.append(t)
            out.append((t.id, di))
        return out

    @staticmethod
    def _hit(t: _Track, box: np.ndarray, score: float, ts: float) -> None:
        t.kf.update(box)
        t.score, t.last_ts, t.hits = float(score), ts, t.hits + 1
        t.state = TRACKED
