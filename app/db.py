"""SQLite persistence layer (stdlib only)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    stored_path   TEXT,
    crs           TEXT,
    feature_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS features (
    file_id             TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    feature_index       INTEGER NOT NULL,
    feature_id          TEXT,
    geometry_type       TEXT NOT NULL,
    geometry_json       TEXT,
    crs                 TEXT,
    properties_json     TEXT NOT NULL,
    measurement_status  TEXT NOT NULL,
    measurement_type    TEXT,
    measurement_value   REAL,
    measurement_unit    TEXT,
    measurement_crs     TEXT,
    measurement_message TEXT,
    PRIMARY KEY (file_id, feature_index)
);
"""


@lru_cache(maxsize=None)
def _ensure_schema(path: str) -> None:
    config.data_dir().mkdir(parents=True, exist_ok=True)
    config.upload_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    _ensure_schema(str(config.db_path()))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error and always closes."""
    path = str(config.db_path())
    _ensure_schema(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_file(
    *, file_id: str, filename: str, stored_path: str | None, crs: str | None,
    feature_count: int, status: str, created_at: str, error: str | None = None,
    features: list[dict[str, Any]] | None = None,
) -> None:
    """Insert a file record and (optionally) all of its features in one transaction."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO files(id, filename, stored_path, crs, feature_count, status, created_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, filename, stored_path, crs, feature_count, status, created_at, error),
        )
        if features:
            conn.executemany(
                """INSERT INTO features(
                       file_id, feature_index, feature_id, geometry_type, geometry_json, crs,
                       properties_json, measurement_status, measurement_type, measurement_value,
                       measurement_unit, measurement_crs, measurement_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        file_id, f["feature_index"], f["feature_id"], f["geometry_type"],
                        json.dumps(f["geometry"]) if f["geometry"] is not None else None,
                        f["crs"], json.dumps(f["properties"]), f["measurement_status"],
                        f["measurement_type"], f["measurement_value"], f["measurement_unit"],
                        f["measurement_crs"], f["measurement_message"],
                    )
                    for f in features
                ],
            )


def get_file(file_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def list_features(file_id: str, limit: int, offset: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM features WHERE file_id = ? ORDER BY feature_index LIMIT ? OFFSET ?",
            (file_id, limit, offset),
        ).fetchall()


def summarize(file_id: str) -> dict[str, Any]:
    with connect() as conn:
        totals = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN measurement_type = 'area'   THEN measurement_value END), 0) AS area,
                 COALESCE(SUM(CASE WHEN measurement_type = 'length' THEN measurement_value END), 0) AS length
               FROM features WHERE file_id = ?""",
            (file_id,),
        ).fetchone()
        by_type = conn.execute(
            "SELECT geometry_type, COUNT(*) AS n FROM features WHERE file_id = ? GROUP BY geometry_type",
            (file_id,),
        ).fetchall()
        by_status = conn.execute(
            "SELECT measurement_status, COUNT(*) AS n FROM features WHERE file_id = ? GROUP BY measurement_status",
            (file_id,),
        ).fetchall()
    return {
        "total_area_m2": round(totals["area"], 4),
        "total_length_m": round(totals["length"], 4),
        "geometry_types": {r["geometry_type"]: r["n"] for r in by_type},
        "measurement_statuses": {r["measurement_status"]: r["n"] for r in by_status},
    }
