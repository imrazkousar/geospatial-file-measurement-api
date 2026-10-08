"""Orchestrates parse -> measure -> persist for one uploaded file."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import mapping

from . import db, measurements
from .parsers import ParseError, RawFeature, crs_label, read_geofile

log = logging.getLogger(__name__)

STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


class ProcessingError(Exception):
    """Raised after a FAILED file record has been stored."""

    def __init__(self, file_id: str, message: str, status_code: int):
        super().__init__(message)
        self.file_id = file_id
        self.message = message
        self.status_code = status_code


def _feature_row(raw: RawFeature, source_crs, crs_text: str) -> dict[str, Any]:
    geom = raw.geometry
    if raw.issue:
        result = measurements.Measurement(measurements.FAILED, message=raw.issue)
    else:
        result = measurements.measure(geom, source_crs)
    return {
        "feature_index": raw.index,
        "feature_id": raw.id,
        "geometry_type": geom.geom_type if geom is not None else "None",
        "geometry": mapping(geom) if geom is not None else None,
        "crs": crs_text,
        "properties": raw.properties,
        "measurement_status": result.status,
        "measurement_type": result.type,
        "measurement_value": result.value,
        "measurement_unit": result.unit,
        "measurement_crs": result.crs,
        "measurement_message": result.message,
    }


def process_file(
    *, file_id: str, filename: str, stored_path: Path, suffix: str, crs_override: str | None
) -> dict[str, Any]:
    """Process an uploaded file synchronously; return the stored file record fields."""
    created_at = datetime.now(timezone.utc).isoformat()

    def fail(message: str, status_code: int) -> ProcessingError:
        stored_path.unlink(missing_ok=True)
        db.insert_file(
            file_id=file_id, filename=filename, stored_path=None, crs=None, feature_count=0,
            status=STATUS_FAILED, created_at=created_at, error=message,
        )
        return ProcessingError(file_id, message, status_code)

    try:
        parsed = read_geofile(stored_path, suffix, crs_override)
        crs_text = crs_label(parsed.crs)
        rows = [_feature_row(f, parsed.crs, crs_text) for f in parsed.features]
        json.dumps([r["properties"] for r in rows])  # fail early if attributes aren't serialisable
    except ParseError as exc:
        raise fail(str(exc), 400) from exc
    except Exception as exc:  # unexpected reader/projection failure: still don't crash the API
        log.exception("Unexpected failure processing %s", filename)
        raise fail(f"Could not process geospatial file: {exc}", 422) from exc

    db.insert_file(
        file_id=file_id, filename=filename, stored_path=str(stored_path), crs=crs_text,
        feature_count=len(rows), status=STATUS_COMPLETED, created_at=created_at, features=rows,
    )
    return {
        "id": file_id, "filename": filename, "feature_count": len(rows), "crs": crs_text,
        "status": STATUS_COMPLETED, "created_at": created_at, "error": None,
    }
