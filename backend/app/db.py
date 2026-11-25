"""SQLite storage for StockDeck.

Tables
------
skus            one row per sellable item *and size* (Tito's 750ml and Tito's 1.75L are two SKUs)
locations       a physical spot: zone (floor/backroom) + aisle + bay
count_sessions  one inventory count (e.g. "Monthly count 2026-11")
captures        one photo of one bay inside a session; rescans of a bay supersede older captures
slots           one detected product group on a shelf inside a capture (the blueprint cell)
blueprint_slots the last *confirmed* layout of each location — the shelf blueprint
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS skus (
    id INTEGER PRIMARY KEY,
    upc TEXT UNIQUE,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    size_ml INTEGER,
    case_pack INTEGER,
    case_upc TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skus_case_upc ON skus(case_upc);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    zone TEXT NOT NULL CHECK (zone IN ('floor', 'backroom')),
    aisle TEXT NOT NULL,
    bay TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS count_sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES count_sessions(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    image_path TEXT NOT NULL,
    recognizer TEXT NOT NULL,
    image_quality TEXT,
    warnings TEXT DEFAULT '[]',
    raw_json TEXT,
    status TEXT NOT NULL DEFAULT 'review' CHECK (status IN ('review', 'confirmed', 'superseded')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('bottle', 'case')),
    shelf INTEGER NOT NULL,
    position INTEGER NOT NULL,
    sku_id INTEGER REFERENCES skus(id),
    raw_brand TEXT DEFAULT '',
    raw_name TEXT DEFAULT '',
    raw_size TEXT DEFAULT '',
    raw_size_ml INTEGER,
    facings INTEGER NOT NULL DEFAULT 0,
    depth INTEGER NOT NULL DEFAULT 1,
    cases INTEGER NOT NULL DEFAULT 0,
    units_per_case INTEGER,
    partial_units INTEGER NOT NULL DEFAULT 0,
    confidence REAL DEFAULT 0,
    match_score REAL DEFAULT 0,
    candidates TEXT DEFAULT '[]',
    box TEXT,
    source TEXT NOT NULL DEFAULT 'vision',
    review TEXT NOT NULL DEFAULT 'auto' CHECK (review IN ('auto', 'needs_review', 'confirmed'))
);

CREATE TABLE IF NOT EXISTS blueprint_slots (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    kind TEXT NOT NULL,
    shelf INTEGER NOT NULL,
    position INTEGER NOT NULL,
    sku_id INTEGER NOT NULL REFERENCES skus(id),
    facings INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    capture_id INTEGER REFERENCES captures(id),
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_blueprint_location ON blueprint_slots(location_id);
"""


def data_dir() -> Path:
    path = Path(os.environ.get("STOCKDECK_DATA", Path(__file__).resolve().parents[2] / "var"))
    path.mkdir(parents=True, exist_ok=True)
    (path / "images").mkdir(exist_ok=True)
    (path / "crops").mkdir(exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "stockdeck.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release; ALTER TABLE keeps existing databases working.
MIGRATIONS = {
    "captures": {
        "mode": "TEXT NOT NULL DEFAULT 'photo'",   # photo | map (walk-through) | count (point-and-count)
        "seq": "INTEGER",                          # order of a snapshot within a walk-through
    },
    "slots": {
        "duplicate_of": "INTEGER",                 # same bottles already counted in the previous overlapping snapshot
        "crop_path": "TEXT",                       # reference photo of this product group
        "blueprint_ref": "TEXT",                   # count mode: which blueprint slot this counts (e.g. F-3-S02/s2p1)
        "note": "TEXT DEFAULT ''",
    },
}


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def location_code(zone: str, aisle: str, bay: str) -> str:
    prefix = "F" if zone == "floor" else "B"
    return f"{prefix}-{aisle.strip().upper()}-{bay.strip().upper()}"


def get_or_create_location(conn: sqlite3.Connection, zone: str, aisle: str, bay: str) -> dict:
    code = location_code(zone, aisle, bay)
    row = conn.execute("SELECT * FROM locations WHERE code = ?", (code,)).fetchone()
    if row:
        return dict(row)
    cur = conn.execute(
        "INSERT INTO locations (zone, aisle, bay, code) VALUES (?, ?, ?, ?)",
        (zone, aisle.strip().upper(), bay.strip().upper(), code),
    )
    return dict(conn.execute("SELECT * FROM locations WHERE id = ?", (cur.lastrowid,)).fetchone())
