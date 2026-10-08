"""Readers for KML and zipped Shapefiles. Both produce the same ``ParsedFile`` structure."""
from __future__ import annotations

import math
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

import geopandas as gpd
import pandas as pd
from defusedxml import ElementTree as SafeET
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import (
    GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon,
)
from shapely.geometry.base import BaseGeometry

from . import config


class ParseError(ValueError):
    """The uploaded file is unreadable or invalid (client error)."""


@dataclass
class RawFeature:
    index: int
    id: str
    geometry: BaseGeometry | None
    properties: dict[str, Any] = field(default_factory=dict)
    issue: str | None = None  # set when this one feature could not be parsed


@dataclass
class ParsedFile:
    features: list[RawFeature]
    crs: CRS


def crs_label(crs: CRS) -> str:
    """Human-friendly CRS identifier, e.g. 'EPSG:4326' (falls back to the full definition)."""
    try:
        authority = crs.to_authority(min_confidence=70)
    except Exception:  # pragma: no cover - defensive
        authority = None
    return f"{authority[0]}:{authority[1]}" if authority else crs.to_string()


# --------------------------------------------------------------------------- KML

_GEOM_TAGS = {"Point", "LineString", "Polygon", "MultiGeometry"}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    found = _children(el, name)
    return found[0] if found else None


def _coordinates(el: ET.Element | None) -> list[tuple[float, float]]:
    """Parse a KML <coordinates> child (lon,lat[,alt] tuples). Altitude is dropped."""
    coords_el = _child(el, "coordinates") if el is not None else None
    text = re.sub(r"\s*,\s*", ",", (coords_el.text or "") if coords_el is not None else "")
    points: list[tuple[float, float]] = []
    for token in text.split():
        parts = token.split(",")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            raise ValueError(f"Malformed coordinate tuple: {token!r}") from None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"Non-finite coordinate: {token!r}")
        points.append((lon, lat))
    return points


def _ring(boundary: ET.Element | None) -> list[tuple[float, float]]:
    ring = _child(boundary, "LinearRing") if boundary is not None else None
    coords = _coordinates(ring)
    if len(coords) < 4:
        raise ValueError("Polygon ring needs at least 4 coordinates.")
    return coords


def _atomic_geometries(el: ET.Element) -> list[BaseGeometry]:
    tag = _local(el.tag)
    if tag == "Point":
        coords = _coordinates(el)
        if len(coords) != 1:
            raise ValueError("Point needs exactly one coordinate.")
        return [Point(coords[0])]
    if tag == "LineString":
        coords = _coordinates(el)
        if len(coords) < 2:
            raise ValueError("LineString needs at least 2 coordinates.")
        return [LineString(coords)]
    if tag == "Polygon":
        outer = _child(el, "outerBoundaryIs")
        if outer is None:
            raise ValueError("Polygon has no outerBoundaryIs.")
        holes = [_ring(inner) for inner in _children(el, "innerBoundaryIs")]
        return [Polygon(_ring(outer), holes)]
    if tag == "MultiGeometry":
        parts: list[BaseGeometry] = []
        for child in el:
            if _local(child.tag) in _GEOM_TAGS:
                parts.extend(_atomic_geometries(child))
        return parts
    return []


def _combine(parts: list[BaseGeometry]) -> BaseGeometry | None:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    kinds = {p.geom_type for p in parts}
    if kinds == {"Point"}:
        return MultiPoint(parts)
    if kinds == {"LineString"}:
        return MultiLineString(parts)
    if kinds == {"Polygon"}:
        return MultiPolygon(parts)
    return GeometryCollection(parts)


def _placemark_properties(placemark: ET.Element) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name in ("name", "description"):
        el = _child(placemark, name)
        if el is not None and el.text and el.text.strip():
            props[name] = el.text.strip()
    extended = _child(placemark, "ExtendedData")
    if extended is not None:
        for el in extended.iter():
            key = el.get("name")
            if not key:
                continue
            if _local(el.tag) == "Data":
                value = _child(el, "value")
                props[key] = value.text.strip() if value is not None and value.text else None
            elif _local(el.tag) == "SimpleData":
                props[key] = el.text.strip() if el.text else None
    return props


def parse_kml(path: Path) -> ParsedFile:
    try:
        root = SafeET.parse(str(path)).getroot()
    except (ET.ParseError, ValueError) as exc:  # defusedxml errors subclass ValueError
        raise ParseError(f"Invalid or unsafe KML XML: {exc}") from exc

    features: list[RawFeature] = []
    for placemark in (el for el in root.iter() if _local(el.tag) == "Placemark"):
        index = len(features)
        geometry, issue = None, None
        try:
            parts: list[BaseGeometry] = []
            for child in placemark:
                if _local(child.tag) in _GEOM_TAGS:
                    parts.extend(_atomic_geometries(child))
            geometry = _combine(parts)
            if geometry is None:
                issue = "Placemark has no supported geometry."
        except ValueError as exc:
            issue = str(exc)
        features.append(RawFeature(
            index=index,
            id=placemark.get("id") or str(index),
            geometry=geometry,
            properties=_placemark_properties(placemark),
            issue=issue,
        ))
    if not features:
        raise ParseError("The KML file contains no Placemark features.")
    return ParsedFile(features, CRS.from_epsg(4326))  # KML is always WGS 84 lon/lat


# ---------------------------------------------------------------------- Shapefile

def _json_safe(value: Any) -> Any:
    """Convert pandas/numpy attribute values to plain JSON-serialisable Python."""
    if value is None or value is pd.NaT:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _extract_zip(path: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise ParseError("The ZIP archive is empty.")
            total = 0
            for info in infos:
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
                    raise ParseError("The ZIP archive contains an unsafe file path.")
                total += info.file_size
            if total > config.max_uncompressed_bytes():
                raise ParseError("The ZIP archive is too large when uncompressed.")
            archive.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise ParseError("The uploaded ZIP file is invalid.") from exc


def parse_shapefile_zip(path: Path, crs_override: str | None = None) -> ParsedFile:
    with tempfile.TemporaryDirectory(prefix="geoapi_") as tmp:
        root = Path(tmp)
        _extract_zip(path, root)
        shapefiles = sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() == ".shp"
            and "__MACOSX" not in p.parts and not p.name.startswith("._")
        )
        if not shapefiles:
            raise ParseError("No .shp file was found inside the ZIP archive.")
        if len(shapefiles) > 1:
            names = ", ".join(p.name for p in shapefiles)
            raise ParseError(f"The ZIP contains multiple Shapefiles ({names}); upload one per archive.")
        try:
            frame = gpd.read_file(shapefiles[0])
        except Exception as exc:
            raise ParseError(f"Could not read the Shapefile (are .shx/.dbf included?): {exc}") from exc

        crs = frame.crs
        if crs is None:
            if not crs_override:
                raise ParseError(
                    "The Shapefile has no CRS (.prj missing). Include the .prj file or "
                    "send a 'crs' form field such as 'EPSG:4326'."
                )
            try:
                crs = CRS.from_user_input(crs_override)
            except CRSError as exc:
                raise ParseError(f"Invalid 'crs' value {crs_override!r}: {exc}") from exc
        else:
            crs = CRS.from_user_input(crs)
        if len(frame) == 0:
            raise ParseError("The Shapefile contains no features.")

        geom_col = frame.geometry.name
        attributes = frame.drop(columns=[geom_col]).to_dict("records")
        features = []
        for i, (geom, attrs) in enumerate(zip(frame.geometry, attributes)):
            features.append(RawFeature(
                index=i,
                id=str(i),
                geometry=None if geom is None else geom,
                properties={str(k): _json_safe(v) for k, v in attrs.items()},
            ))
        return ParsedFile(features, crs)


def read_geofile(path: Path, suffix: str, crs_override: str | None = None) -> ParsedFile:
    if suffix == ".kml":
        return parse_kml(path)
    if suffix == ".zip":
        return parse_shapefile_zip(path, crs_override)
    raise ParseError("Only .zip Shapefile archives and .kml files are supported.")
