"""Pydantic response models (drive the OpenAPI docs)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: str
    created_at: str
    error: str | None = None


class UploadResponse(FileInfo):
    measurements_url: str


class MeasurementOut(BaseModel):
    status: str  # COMPLETED | NOT_REQUIRED | UNSUPPORTED | FAILED
    type: str | None = None  # area | length
    value: float | None = None
    unit: str | None = None
    crs: str | None = None  # projected CRS used for the calculation
    message: str | None = None


class FeatureOut(BaseModel):
    feature_index: int
    feature_id: str | None
    geometry_type: str
    geometry: dict[str, Any] | None
    crs: str | None
    properties: dict[str, Any]
    measurement: MeasurementOut


class Summary(BaseModel):
    total_area_m2: float
    total_length_m: float
    geometry_types: dict[str, int]
    measurement_statuses: dict[str, int]


class MeasurementsResponse(BaseModel):
    file_id: str
    crs: str | None
    feature_count: int  # total features in the file
    offset: int
    limit: int
    summary: Summary
    features: list[FeatureOut]
