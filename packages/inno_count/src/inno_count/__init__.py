"""inno_count — the one shared counting core (prompt §3 rule 1, §7.3).

Imported by the ERP Hardware Agent and by the studio harness, so what is measured is what runs.

What is here (v1 core):
  types       Detection and the events sent to the ERP
  geometry    letterbox, line side, truncation, plane homography for size in cm
  bytetrack   our own ByteTrack (Kalman + two-stage IoU association), time-based
  legacy      the old greedy centroid tracker, kept only as a gate baseline / mutation test
  counter     line crossing, per-track crops and vote, size at the line, health per minute
  camera      "camera moved" check against the stored reference frame
  detector    ONNX detector with letterbox and vectorised decode (needs the `onnx` extra)
  engine      runs detector + tracker + counter at a fixed rate on stream timestamps
  sim         synthetic belt for tests and demos

Not here yet: v0 (the agent's legacy code extracted verbatim). It needs the ERP repository.

Rule: no AGPL dependency may enter this package (prompt rule 9, decision D6).
"""

__version__ = "0.1.0"

from .types import ClassificationEvent, CountEvent, Detection, HealthEvent  # noqa: E402

__all__ = ["ClassificationEvent", "CountEvent", "Detection", "HealthEvent", "__version__"]
