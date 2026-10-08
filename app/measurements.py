"""CRS selection and area/length measurement.

Measurements are never computed in degrees. Every geometry is reprojected to the UTM zone
(or polar stereographic CRS) that contains its bounding-box centre, then measured in metres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import force_2d
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import explain_validity

WGS84 = CRS.from_epsg(4326)

COMPLETED = "COMPLETED"
NOT_REQUIRED = "NOT_REQUIRED"
UNSUPPORTED = "UNSUPPORTED"
FAILED = "FAILED"


@dataclass(frozen=True)
class Measurement:
    status: str
    type: str | None = None  # "area" | "length"
    value: float | None = None
    unit: str | None = None
    crs: str | None = None  # projected CRS used for the calculation
    message: str | None = None


def _atoms(geom: BaseGeometry) -> Iterator[BaseGeometry]:
    """Yield the non-collection parts of a (multi)geometry."""
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _atoms(part)
    else:
        yield geom


_FAMILY = {
    "Point": "point",
    "LineString": "line",
    "LinearRing": "line",
    "Polygon": "polygon",
}


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone for a lon/lat, falling back to UPS beyond 84°N / 80°S."""
    if lat > 84:
        return 32661  # WGS 84 / UPS North
    if lat < -80:
        return 32761  # WGS 84 / UPS South
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def _finite_bounds(geom: BaseGeometry) -> tuple[float, float, float, float]:
    bounds = geom.bounds
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError("Coordinates could not be transformed (non-finite values).")
    return bounds


def to_local_projected(geom: BaseGeometry, source: CRS) -> tuple[BaseGeometry, str]:
    """Project ``geom`` (in ``source`` CRS, x=lon/easting) to a metric CRS; return it and its label."""
    geom = force_2d(geom)
    if source == WGS84:
        geographic = geom
    else:
        to_wgs84 = Transformer.from_crs(source, WGS84, always_xy=True)
        geographic = transform(to_wgs84.transform, geom)
    minx, miny, maxx, maxy = _finite_bounds(geographic)
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise ValueError("Coordinates fall outside valid longitude/latitude ranges.")

    epsg = utm_epsg((minx + maxx) / 2, (miny + maxy) / 2)
    target = CRS.from_epsg(epsg)
    to_target = Transformer.from_crs(source, target, always_xy=True)
    projected = transform(to_target.transform, geom)
    _finite_bounds(projected)
    return projected, f"EPSG:{epsg}"


def measure(geom: BaseGeometry | None, source: CRS) -> Measurement:
    """Measure a geometry: area for polygons, length for lines, nothing for points."""
    if geom is None:
        return Measurement(UNSUPPORTED, message="Feature has no geometry.")
    if geom.is_empty:
        return Measurement(UNSUPPORTED, message="Geometry is empty.")

    families = {_FAMILY.get(a.geom_type) for a in _atoms(geom)}
    if None in families:
        return Measurement(UNSUPPORTED, message=f"Unsupported geometry type: {geom.geom_type}.")
    if families == {"point"}:
        return Measurement(NOT_REQUIRED)
    if len(families) > 1:
        return Measurement(
            UNSUPPORTED,
            message="Mixed geometry types in one feature (e.g. polygon + line) are not measured.",
        )

    try:
        projected, crs_label = to_local_projected(geom, source)
    except (ValueError, ProjError) as exc:
        return Measurement(FAILED, message=f"Could not project geometry: {exc}")

    if families == {"polygon"}:
        message = None
        if not geom.is_valid:
            message = f"Geometry is invalid ({explain_validity(geom)}); area may be unreliable."
        return Measurement(
            COMPLETED, "area", round(float(projected.area), 4), "m²", crs_label, message
        )
    return Measurement(COMPLETED, "length", round(float(projected.length), 4), "m", crs_label)
