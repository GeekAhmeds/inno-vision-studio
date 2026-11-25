"""Camera-moved check (prompt §7.3, acceptance test 9).

The live frame is compared with the stored reference frame on edges, not on brightness, so day and
night of the same angle agree. Regions that change by themselves (a burned-in clock) are masked
out. A shift larger than `max_shift_frac` of the frame width, or a frame with almost no edges (the
lens covered), must be seen on `confirm` checks in a row; then "camera moved" is latched until a
new reference is stored.
"""

from __future__ import annotations

import math

import cv2
import numpy as np


def _edges(image: np.ndarray, work_width: int, mask: np.ndarray | None) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape
    size = (work_width, max(1, round(h * work_width / w)))
    gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.float32)
    gray = cv2.GaussianBlur(gray, (5, 5), 1.2)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    if mask is not None:
        small = cv2.resize(mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
        mag[small] = 0
    mean = float(mag.mean())
    return mag / mean if mean > 1e-6 else mag


class CameraCheck:
    def __init__(self, reference: np.ndarray, mask: np.ndarray | None = None, max_shift_frac: float = 0.02,
                 every_s: float = 10.0, confirm: int = 2, work_width: int = 320, min_edge_ratio: float = 0.15):
        self.mask, self.work_width = mask, work_width
        self.max_shift_frac, self.every_s, self.confirm = max_shift_frac, every_s, confirm
        self.min_edge_ratio = min_edge_ratio
        self.moved = False
        self.last_shift_px: float = 0.0
        self.last_reason = ""
        self._bad = 0
        self._next_check: float | None = None
        self.set_reference(reference)

    def set_reference(self, reference: np.ndarray) -> None:
        self._ref_w = reference.shape[1]
        self._ref_raw_edges = self._raw_edge_level(reference)
        self._ref = _edges(reference, self.work_width, self.mask)
        self._window = cv2.createHanningWindow(self._ref.shape[::-1], cv2.CV_32F)
        self.moved, self._bad = False, 0

    def _raw_edge_level(self, image: np.ndarray) -> float:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        gray = cv2.resize(gray, (self.work_width, max(1, round(gray.shape[0] * self.work_width / gray.shape[1]))))
        lap = cv2.Laplacian(cv2.GaussianBlur(gray, (5, 5), 1.2).astype(np.float32), cv2.CV_32F)
        return float(np.abs(lap).mean() / max(float(gray.mean()), 1.0))

    def measure(self, frame: np.ndarray) -> tuple[bool, float, str]:
        """One comparison, without the timing and confirmation logic."""
        if self._raw_edge_level(frame) < self.min_edge_ratio * self._ref_raw_edges:
            return True, math.inf, "covered or out of focus"
        cur = _edges(frame, self.work_width, self.mask)
        (dx, dy), _ = cv2.phaseCorrelate(self._ref, cur, self._window)
        shift = math.hypot(dx, dy) * self._ref_w / self.work_width
        return shift > self.max_shift_frac * self._ref_w, shift, "shifted" if shift else ""

    def check(self, ts: float, frame: np.ndarray) -> bool:
        if self._next_check is not None and ts < self._next_check:
            return self.moved
        self._next_check = ts + self.every_s
        bad, shift, reason = self.measure(frame)
        self.last_shift_px, self.last_reason = shift, reason if bad else ""
        self._bad = self._bad + 1 if bad else 0
        if self._bad >= self.confirm:
            self.moved = True
        return self.moved
