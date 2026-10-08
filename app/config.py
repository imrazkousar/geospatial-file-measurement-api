"""Runtime configuration, read lazily from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", _PROJECT_ROOT / "data"))


def upload_dir() -> Path:
    return data_dir() / "uploads"


def db_path() -> Path:
    return data_dir() / "measurements.sqlite3"


def max_upload_bytes() -> int:
    """Maximum size of an uploaded file (default 50 MiB)."""
    return int(os.getenv("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))


def max_uncompressed_bytes() -> int:
    """Maximum total uncompressed size of a ZIP upload (zip-bomb guard, default 500 MiB)."""
    return int(os.getenv("MAX_UNCOMPRESSED_BYTES", 500 * 1024 * 1024))
