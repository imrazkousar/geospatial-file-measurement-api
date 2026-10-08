import pytest
from pyproj import CRS
from shapely.geometry import GeometryCollection, LineString, MultiPoint, Point, Polygon

from app import measurements as m

WGS84 = CRS.from_epsg(4326)


@pytest.mark.parametrize("lon,lat,epsg", [
    (77.5, 12.9, 32643), (-122.4, 37.8, 32610), (151.2, -33.9, 32756),
    (0, 89, 32661), (0, -85, 32761),
])
def test_utm_epsg(lon, lat, epsg):
    assert m.utm_epsg(lon, lat) == epsg


def test_polygon_area_is_in_square_metres():
    poly = Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)])  # ~1.11 km square at the equator
    r = m.measure(poly, WGS84)
    assert r.status == m.COMPLETED and r.type == "area" and r.unit == "m²"
    assert 1.2e6 < r.value < 1.3e6


def test_line_length_one_degree_of_latitude():
    r = m.measure(LineString([(10, 0), (10, 1)]), WGS84)
    assert r.type == "length" and r.unit == "m"
    assert r.value == pytest.approx(110_574, rel=0.01)


def test_points_not_required():
    assert m.measure(Point(1, 1), WGS84).status == m.NOT_REQUIRED
    assert m.measure(MultiPoint([(1, 1), (2, 2)]), WGS84).status == m.NOT_REQUIRED


def test_unsupported_and_empty_and_none():
    assert m.measure(None, WGS84).status == m.UNSUPPORTED
    assert m.measure(Polygon(), WGS84).status == m.UNSUPPORTED
    mixed = GeometryCollection([Point(1, 1), LineString([(0, 0), (1, 1)])])
    assert m.measure(mixed, WGS84).status == m.UNSUPPORTED


def test_out_of_range_coordinates_fail_gracefully():
    assert m.measure(LineString([(500, 0), (501, 1)]), WGS84).status == m.FAILED


def test_invalid_polygon_flagged():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    r = m.measure(bowtie, WGS84)
    assert r.status == m.COMPLETED and "invalid" in r.message.lower()
