"""Harness. `match` is imported here; the runner lives in `studio.harness.run` (it imports the
truth store, which itself uses `match`, so it is not imported at package level)."""

from .match import MatchResult, match, wilson

__all__ = ["MatchResult", "match", "wilson"]
