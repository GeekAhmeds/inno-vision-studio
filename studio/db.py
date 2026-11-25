"""Per-project SQLite store: media index and labels with provenance.

Connections are opened per operation and always closed, so Windows can move or delete the file.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_FILE = "studio.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('video', 'image')),
    rel_path    TEXT,            -- posix path inside the project, when copied
    linked_path TEXT,            -- absolute path outside the project, when linked in place
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    width       INTEGER,
    height      INTEGER,
    fps         REAL,
    frames      INTEGER,
    duration_s  REAL,
    source_id   TEXT,
    angle_id    TEXT,
    origin_name TEXT,
    added_at    TEXT NOT NULL,
    UNIQUE (sha256)
);
CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY,
    media_id    INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    class_id    TEXT NOT NULL,
    bbox        TEXT,            -- JSON [x, y, w, h] in pixels; NULL = whole-image class label
    provenance  TEXT NOT NULL,   -- auto:<model> | claude:<id>@<prompt> | imported:<src> | human:<user>
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS labels_media ON labels(media_id);

-- Truth: short clips counted by humans; the only scoreboard (prompt §6 module 8).
CREATE TABLE IF NOT EXISTS truth_clips (
    id          INTEGER PRIMARY KEY,
    media_id    INTEGER NOT NULL REFERENCES media(id),
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,
    condition   TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    final_taps  TEXT,            -- JSON list after reconciliation; NULL until then
    final_by    TEXT,
    locked      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS truth_counts (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES truth_clips(id) ON DELETE CASCADE,
    counter     TEXT NOT NULL,   -- who counted
    finished    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    taps        TEXT NOT NULL DEFAULT '[]'   -- JSON [{t, class_id, x, y}]
);
"""


_ready: set[str] = set()


@contextmanager
def connect(project_dir: Path) -> Iterator[sqlite3.Connection]:
    path = project_dir / DB_FILE
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    key = str(path.resolve())
    if key not in _ready:  # bring older project databases up to the current schema
        con.executescript(SCHEMA)
        _ready.add(key)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(project_dir: Path) -> None:
    with connect(project_dir) as con:
        con.executescript(SCHEMA)
    _ready.add(str((project_dir / DB_FILE).resolve()))


def forget(project_dir: Path) -> None:
    """Call when a project folder is deleted or replaced, so its schema is checked again."""
    _ready.discard(str((project_dir / DB_FILE).resolve()))
