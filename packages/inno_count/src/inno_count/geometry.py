"""Geometry shared by training, the agent and the harness."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .types import Detection


# ------------------------------------------------------------------ letterbox

@dataclass(frozen=True)
class LetterboxInfo:
    scale: float
    pad_x: float
    pad_y: float


def letterbox(image: np.ndarray, size: int, colour: int = 114) -> tuple[np.ndarray, LetterboxInfo]:
    """Resize keeping the aspect ratio and pad to size×size (matches Ultralytics training).

    The legacy agent stretched the image instead (audit §1 item 3); a model trained on letterboxed
    images sees distorted objects when stretched.
    """
    h, w = image.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = round(w * scale), round(h * scale)
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR) if (nw, nh) != (w, h) else image
    pad_x, pad_y = (size - nw) / 2, (size - nh) / 2
    top, left = round(pad_y - 0.1), round(pad_x - 0.1)
    out = cv2.copyMakeBorder(resized, top, size - nh - top, left, size - nw - left,
                             cv2.BORDER_CONSTANT, value=(colour, colour, colour))
    return out, LetterboxInfo(scale, left, top)


def unletterbox(boxes: np.ndarray, info: LetterboxInfo) -> np.ndarray:
    """Map xyxy boxes from letterbox space back to the original image."""
    out = boxes.astype(np.float64).copy()
    out[:, [0, 2]] = (out[:, [0, 2]] - info.pad_x) / info.scale
    out[:, [1, 3]] = (out[:, [1, 3]] - info.pad_y) / info.scale
    return out


# ------------------------------------------------------------------ line

@dataclass(frozen=True)
class Line:
    """Counting line from p1 to p2. Signed distance > 0 is the "after" side (left of p1→p2 in
    image coordinates, i.e. y grows downward)."""

    x1: float
    y1: float
    x2: float
    y2: float

    def signed_distance(self, x: float, y: float) -> float:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        norm = math.hypot(dx, dy)
        if norm == 0:
            raise ValueError("Counting line has zero length.")
        return ((x - self.x1) * dy - (y - self.y1) * dx) / norm


# ------------------------------------------------------------------ truncation / overlap

def is_truncated(det: Detection, bounds: tuple[float, float, float, float], margin: float) -> bool:
    """A box touching the ROI or frame edge is cut: never used for size or type (prompt §7.2)."""
    bx1, by1, bx2, by2 = bounds
    return (det.x1 <= bx1 + margin or det.y1 <= by1 + margin
            or det.x2 >= bx2 - margin or det.y2 >= by2 - margin)


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between every box in a (N×4 xyxy) and b (M×4), vectorised."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
    iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
    iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)


# ------------------------------------------------------------------ size on the plane

class PlaneCalibration:
    """Homography from image pixels to the belt plane in centimetres (prompt §7.2).

    The owner clicks four points on the belt (a rectangle on the real surface) and enters its
    real width and length. Works for any product, because it calibrates the surface, not the object.
    """

    def __init__(self, image_points: list[tuple[float, float]], width_cm: float, length_cm: float):
        if len(image_points) != 4:
            raise ValueError("Calibration needs exactly four points, in order around the rectangle.")
        if width_cm <= 0 or length_cm <= 0:
            raise ValueError("Real distances must be positive.")
        src = np.asarray(image_points, dtype=np.float64)
        dst = np.array([[0, 0], [width_cm, 0], [width_cm, length_cm], [0, length_cm]], dtype=np.float64)
        self.H = cv2.getPerspectiveTransform(src.astype(np.float32), dst.astype(np.float32)).astype(np.float64)
        self.image_points = [tuple(map(float, p)) for p in image_points]
        self.width_cm, self.length_cm = float(width_cm), float(length_cm)

    def to_plane(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(pts, self.H).reshape(-1, 2)

    def box_size_cm(self, det: Detection) -> tuple[float, float]:
        """Width and length in cm of the box footprint: mean of opposite projected edges."""
        c = self.to_plane(np.array([[det.x1, det.y1], [det.x2, det.y1], [det.x2, det.y2], [det.x1, det.y2]]))
        top, bottom = np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[3])
        left, right = np.linalg.norm(c[3] - c[0]), np.linalg.norm(c[2] - c[1])
        return float((top + bottom) / 2), float((left + right) / 2)

    def to_dict(self) -> dict:
        return {"image_points": self.image_points, "width_cm": self.width_cm, "length_cm": self.length_cm}

    @classmethod
    def from_dict(cls, data: dict) -> "PlaneCalibration":
        return cls(data["image_points"], data["width_cm"], data["length_cm"])
