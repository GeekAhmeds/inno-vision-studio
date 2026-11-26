"""Data passed between the detector, tracker, counter and the ERP (prompt §8)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Detection:
    """One box in frame pixels. `cls` is only meaningful for a multi-class detector (gate baseline)."""

    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    cls: int = 0

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def as_xyxy(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass
class CountEvent:
    """Sent at the crossing. Count never waits on type (prompt §7.1)."""

    uid: str
    ts: float                 # stream time of the crossing, seconds (the agent adds the UTC start)
    track_id: int
    model_sha: str
    counting_core_version: str
    x: float | None = None    # where the object crossed the line, frame pixels
    y: float | None = None
    kind: str = "count"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntryEvent:
    """An object crossed the entry line (e.g. the oven mouth). Not a count.

    With an entry line and the counting (exit) line on the same tracker, entries minus counts over a
    period is what was taken off the belt between the two lines (by hand, or dropped).
    """

    uid: str
    ts: float
    track_id: int
    x: float | None = None
    y: float | None = None
    kind: str = "entry"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClassificationEvent:
    """Follows the count with the same uid. A verdict never rewrites a count."""

    uid: str
    ts: float                 # stream time the decision was made
    cls: str                  # class id, or "unknown"
    class_confidence: float
    verdict: str              # whole | partial | two_objects | not_product | unknown
    size_at_line: dict[str, Any] | None
    n_crops: int
    crops: list[Any] = field(default_factory=list, repr=False)  # image arrays; not serialised
    kind: str = "classification"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("crops")
        return data


@dataclass
class HealthEvent:
    """One per minute of stream time, sent with the counts so the ERP can judge a period."""

    minute_start: float
    frames: int
    blind_seconds: float
    reconnects: int
    camera_moved: bool
    kind: str = "health"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
