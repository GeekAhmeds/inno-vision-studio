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
"""


@contextmanager
def connect(project_dir: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(project_dir / DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(project_dir: Path) -> None:
    with connect(project_dir) as con:
        con.executescript(SCHEMA)
