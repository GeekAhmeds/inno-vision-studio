"""Workspace location. Everything a project owns lives under the workspace, never in git."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "INNO_STUDIO_HOME"
ENV_USER = "INNO_STUDIO_USER"


def workspace_dir() -> Path:
    root = os.environ.get(ENV_HOME)
    path = Path(root) if root else Path.home() / "InnoVisionStudio"
    (path / "projects").mkdir(parents=True, exist_ok=True)
    return path


def projects_dir() -> Path:
    return workspace_dir() / "projects"


def current_user() -> str:
    """Name written into `human:<user>` provenance and the activity log."""
    name = os.environ.get(ENV_USER)
    if name:
        return name
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "owner"
