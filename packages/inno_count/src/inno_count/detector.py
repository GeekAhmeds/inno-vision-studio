"""ONNX detector: letterbox, run, vectorised decode, NMS (prompt §7.3).

A model that fails to load raises ModelLoadError with the reason. There is no silent fallback to
motion counting (audit §1 item 3).

Sidecar JSON next to the model (`model.onnx` -> `model.json`), fields used here:
  names     list of class names (or {index: name})
  imgsz     input size (square)
  conf, iou thresholds
  roi_crop  [x1, y1, x2, y2] crop applied before the detector; pixels, or fractions if all <= 1
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .geometry import letterbox, unletterbox
from .types import Detection


class ModelLoadError(RuntimeError):
    """The model cannot be used. The message says why, in words the owner can act on."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode(output: np.ndarray, conf: float, iou: float, nc: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode a YOLO head into (boxes xyxy, scores, classes) in model-input pixels.

    Accepts the two common layouts:
      (1, 4+nc, N) or (1, N, 4+nc): cx, cy, w, h, then one score per class (YOLOv8 / YOLO11);
      (1, N, 6): x1, y1, x2, y2, score, class, already NMS-free (YOLOv10 / YOLO26 end-to-end).
    `nc` (number of classes, from the sidecar) removes any doubt about the layout; without it the
    longer axis is taken as the box axis, which holds for real heads (thousands of anchors).
    """
    out = np.asarray(output)
    if out.ndim == 3:
        out = out[0]
    if out.ndim != 2:
        raise ValueError(f"Unexpected detector output shape {np.asarray(output).shape}")
    if nc is not None and out.shape[0] == 4 + nc:
        channels_first, end_to_end = True, False
    elif nc is not None and out.shape[1] == 4 + nc:
        channels_first, end_to_end = False, False
    else:
        end_to_end = out.shape[1] == 6 and out.shape[0] != 6
        channels_first = not end_to_end and out.shape[0] < out.shape[1]
    if end_to_end:
        boxes, scores, classes = out[:, :4], out[:, 4], out[:, 5].astype(int)
        keep = scores >= conf
        return boxes[keep].astype(np.float64), scores[keep].astype(np.float64), classes[keep]
    if channels_first:
        out = out.T  # (4+nc, N) -> (N, 4+nc)
    cls_scores = out[:, 4:]
    if cls_scores.shape[1] == 0:
        raise ValueError("Detector output has no class scores.")
    classes = cls_scores.argmax(axis=1)
    scores = cls_scores[np.arange(len(out)), classes]
    keep = scores >= conf
    xywh, scores, classes = out[keep, :4], scores[keep], classes[keep]
    boxes = np.empty_like(xywh, dtype=np.float64)
    boxes[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    boxes[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    boxes[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    boxes[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    if len(boxes) == 0:
        return boxes, scores.astype(np.float64), classes
    idx = cv2.dnn.NMSBoxesBatched([[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes],
                                  scores.astype(float).tolist(), classes.astype(int).tolist(), conf, iou)
    idx = np.asarray(idx, dtype=int).reshape(-1)
    return boxes[idx], scores[idx].astype(np.float64), classes[idx]


PREFERRED_PROVIDERS = ["CUDAExecutionProvider", "DmlExecutionProvider", "OpenVINOExecutionProvider",
                       "CoreMLExecutionProvider", "CPUExecutionProvider"]


def local_providers(ort: Any) -> list[str]:
    """Run on this machine only: GPU if the installed onnxruntime has it, then CPU. Never a remote
    provider (some builds list "AzureExecutionProvider" first)."""
    available = set(ort.get_available_providers())
    chosen = [p for p in PREFERRED_PROVIDERS if p in available]
    return chosen or ["CPUExecutionProvider"]


def load_sidecar(model_path: Path) -> dict[str, Any]:
    side = model_path.with_suffix(".json")
    if not side.exists():
        return {}
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelLoadError(f"The sidecar {side.name} is not valid JSON: {exc}") from exc


class OnnxDetector:
    def __init__(self, model_path: str | Path, conf: float | None = None, iou: float | None = None,
                 providers: list[str] | None = None, low_conf: float | None = None):
        path = Path(model_path)
        if not path.is_file():
            raise ModelLoadError(f"Model file not found: {path}")
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise ModelLoadError("onnxruntime is not installed. Install the core with: pip install 'inno-count[onnx]'") from exc
        self.sidecar = load_sidecar(path)
        try:
            self.session = ort.InferenceSession(str(path), providers=providers or local_providers(ort))
        except Exception as exc:  # onnxruntime raises several unrelated types
            raise ModelLoadError(f"The model could not be loaded: {exc}") from exc
        self.sha256 = file_sha256(path)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape_size = inp.shape[2] if isinstance(inp.shape[2], int) else None
        self.imgsz = int(self.sidecar.get("imgsz") or shape_size or 640)
        # The tracker's second round needs low-score boxes, so decode below the publish threshold.
        self.conf = float(conf if conf is not None else self.sidecar.get("conf", 0.25))
        self.decode_conf = float(low_conf if low_conf is not None else min(self.conf, 0.1))
        self.iou = float(iou if iou is not None else self.sidecar.get("iou", 0.5))
        names = self.sidecar.get("names") or []
        self.names = [names[k] for k in sorted(names, key=int)] if isinstance(names, dict) else list(names)
        self.roi_crop = self.sidecar.get("roi_crop")
        self.provider = self.session.get_providers()[0]

    def _roi(self, frame: np.ndarray) -> tuple[int, int, int, int]:
        h, w = frame.shape[:2]
        if not self.roi_crop:
            return 0, 0, w, h
        x1, y1, x2, y2 = self.roi_crop
        if max(x1, y1, x2, y2) <= 1.0:
            x1, x2, y1, y2 = x1 * w, x2 * w, y1 * h, y2 * h
        return int(x1), int(y1), int(x2), int(y2)

    def detect(self, frame: np.ndarray, ts: float | None = None) -> list[Detection]:
        x1, y1, x2, y2 = self._roi(frame)
        crop = frame[y1:y2, x1:x2]
        img, info = letterbox(crop, self.imgsz)
        blob = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        output = self.session.run(None, {self.input_name: blob})[0]
        boxes, scores, classes = decode(output, self.decode_conf, self.iou, len(self.names) or None)
        boxes = unletterbox(boxes, info)
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, crop.shape[1]) + x1
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, crop.shape[0]) + y1
        return [Detection(*map(float, b), float(s), int(c)) for b, s, c in zip(boxes, scores, classes)]
