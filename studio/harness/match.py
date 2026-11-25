"""Event-to-truth matching (prompt rule 5, §6 module 9).

Totals hide errors: 100 counted for 100 real can be 5 missed plus 5 doubled. So every system event
is matched one to one with a human tap inside a time window. An unmatched tap is a MISS; an
unmatched event is an EXTRA (a double count or a false count). Both are reported, never netted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

UNKNOWN = "unknown"


@dataclass
class MatchResult:
    truth: int
    events: int
    matched: list[tuple[int, int]]          # (truth index, event index)
    misses: list[int]                       # truth indexes
    extras: list[int]                       # event indexes
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)  # truth class -> event class -> n

    @property
    def miss_rate(self) -> float:
        return len(self.misses) / self.truth if self.truth else 0.0

    @property
    def extra_rate(self) -> float:
        return len(self.extras) / self.events if self.events else 0.0

    def summary(self) -> dict[str, Any]:
        lo_m, hi_m = wilson(len(self.misses), self.truth)
        lo_e, hi_e = wilson(len(self.extras), self.events)
        typed = sum(n for t, row in self.confusion.items() for e, n in row.items() if e != UNKNOWN)
        right = sum(row.get(t, 0) for t, row in self.confusion.items())
        return {
            "truth": self.truth,
            "events": self.events,
            "matched": len(self.matched),
            "misses": len(self.misses),
            "extras": len(self.extras),
            "miss_rate": round(self.miss_rate, 4),
            "miss_rate_ci95": [round(lo_m, 4), round(hi_m, 4)],
            "extra_rate": round(self.extra_rate, 4),
            "extra_rate_ci95": [round(lo_e, 4), round(hi_e, 4)],
            "type_accuracy": round(right / typed, 4) if typed else None,
            "type_unknown": sum(row.get(UNKNOWN, 0) for row in self.confusion.values()),
            "confusion": self.confusion,
        }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for k successes in n trials."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def match(truth_t: list[float], event_t: list[float], window_s: float = 1.5,
          truth_cls: list[str] | None = None, event_cls: list[str] | None = None,
          truth_xy: list[tuple[float, float] | None] | None = None,
          event_xy: list[tuple[float, float] | None] | None = None, window_px: float = 40.0) -> MatchResult:
    """One-to-one matching inside ±window_s, minimising the total difference.

    Time alone cannot separate a double count from a miss in a pile: two loaves cross within half a
    second, so a duplicate event of one loaf pairs with the other loaf's tap and both errors vanish.
    When the human tapped ON the object (truth_xy) and the event carries where it crossed
    (event_xy), a pair must also be within window_px, and the cost combines time and place.
    """
    t = np.asarray(truth_t, dtype=np.float64)
    e = np.asarray(event_t, dtype=np.float64)
    use_xy = truth_xy is not None and event_xy is not None
    if use_xy:
        txy = np.array([p if p is not None else (np.nan, np.nan) for p in truth_xy], dtype=np.float64).reshape(-1, 2)
        exy = np.array([p if p is not None else (np.nan, np.nan) for p in event_xy], dtype=np.float64).reshape(-1, 2)
    pairs: list[tuple[int, int]] = []
    if len(t) and len(e):
        # Split into independent blocks wherever there is a gap wider than the window,
        # so a full day of events never builds one huge matrix.
        items = sorted([(v, 0, i) for i, v in enumerate(t)] + [(v, 1, i) for i, v in enumerate(e)])
        block: list[tuple[float, int, int]] = []
        for item in items + [(math.inf, -1, -1)]:
            if block and item[0] - block[-1][0] > window_s:
                ti = [i for _, kind, i in block if kind == 0]
                ei = [i for _, kind, i in block if kind == 1]
                if ti and ei:
                    dt = np.abs(t[ti][:, None] - e[ei][None, :])
                    ok = dt <= window_s
                    cost = dt / window_s
                    if use_xy:
                        dist = np.linalg.norm(txy[ti][:, None, :] - exy[ei][None, :, :], axis=2)
                        known = ~np.isnan(dist)
                        ok &= ~known | (dist <= window_px)
                        cost = cost + np.where(known, dist / window_px, 0.0)
                    big = 1e6
                    cost = np.where(ok, cost, big)
                    rows, cols = linear_sum_assignment(cost)
                    pairs += [(ti[r], ei[c]) for r, c in zip(rows, cols) if cost[r, c] < big]
                block = []
            if item[1] >= 0:
                block.append(item)
    pairs.sort()
    used_t = {a for a, _ in pairs}
    used_e = {b for _, b in pairs}
    confusion: dict[str, dict[str, int]] = {}
    if truth_cls is not None:
        for a, b in pairs:
            tc = truth_cls[a] or UNKNOWN
            ec = (event_cls[b] if event_cls is not None else None) or UNKNOWN
            confusion.setdefault(tc, {}).setdefault(ec, 0)
            confusion[tc][ec] += 1
    return MatchResult(len(t), len(e), pairs, [i for i in range(len(t)) if i not in used_t],
                       [i for i in range(len(e)) if i not in used_e], confusion)
