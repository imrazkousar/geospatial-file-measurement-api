import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

SAMPLE_KML = Path(__file__).resolve().parent.parent / "samples" / "sample.kml"


def upload(client, name, data, **form):
    return client.post("/api/files/", files={"file": (name, data)}, data=form)


def zip_shapefile(tmp_path, gdf, name="layer", extra=None):
    shp_dir = tmp_path / "shp"
    shp_dir.mkdir(exist_ok=True)
    gdf.to_file(shp_dir / f"{name}.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in shp_dir.iterdir():
            zf.write(f, f.name)
        for n, content in (extra or {}).items():
            zf.writestr(n, content)
    return buf.getvalue()


def measurements(client, file_id):
    r = client.get(f"/api/files/{file_id}/measurements/")
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    assert client.get("/health/").json() == {"status": "ok"}


def test_reject_unsupported_extension(client):
    assert upload(client, "notes.txt", b"hello").status_code == 400


def test_reject_empty_file(client):
    assert upload(client, "a.kml", b"").status_code == 400


def test_unknown_file_404(client):
    assert client.get("/api/files/nope/").status_code == 404
    assert client.get("/api/files/nope/measurements/").status_code == 404


def test_kml_end_to_end(client):
    r = upload(client, "survey.kml", SAMPLE_KML.read_bytes())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["feature_count"] == 3 and body["crs"] == "EPSG:4326" and body["status"] == "COMPLETED"

    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["filename"] == "survey.kml" and info["feature_count"] == 3

    data = measurements(client, body["id"])
    poly, line, point = data["features"]
    assert poly["geometry_type"] == "Polygon" and poly["properties"]["plot"] == "A-01"
    assert poly["measurement"]["type"] == "area" and poly["measurement"]["unit"] == "m²"
    # ~0.001° x 0.001° at 12.9°N is about 12,000 m² (raw degrees would give ~1e-6).
    assert 11_800 < poly["measurement"]["value"] < 12_200
    assert poly["measurement"]["crs"] == "EPSG:32643"
    assert line["measurement"]["type"] == "length"
    assert 300 < line["measurement"]["value"] < 320
    assert point["measurement"]["status"] == "NOT_REQUIRED" and point["measurement"]["value"] is None
    assert data["summary"]["total_area_m2"] == pytest.approx(poly["measurement"]["value"])


def test_kml_pagination(client):
    fid = upload(client, "s.kml", SAMPLE_KML.read_bytes()).json()["id"]
    r = client.get(f"/api/files/{fid}/measurements/", params={"limit": 1, "offset": 1}).json()
    assert r["feature_count"] == 3 and len(r["features"]) == 1
    assert r["features"][0]["feature_index"] == 1


KML_EDGE = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
 <Placemark id="multi"><MultiGeometry>
  <Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 0,0.01 0.01,0.01 0.01,0 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon>
  <Polygon><outerBoundaryIs><LinearRing><coordinates>1,1 1,1.01 1.01,1.01 1.01,1 1,1</coordinates></LinearRing></outerBoundaryIs></Polygon>
 </MultiGeometry></Placemark>
 <Placemark><name>mixed</name><MultiGeometry>
  <Point><coordinates>1,1</coordinates></Point>
  <LineString><coordinates>1,1 1,2</coordinates></LineString>
 </MultiGeometry></Placemark>
 <Placemark><name>bad</name><LineString><coordinates>1,1</coordinates></LineString></Placemark>
 <Placemark><name>nogeom</name></Placemark>
</Document></kml>"""


def test_kml_edge_cases_do_not_crash(client):
    r = upload(client, "edge.kml", KML_EDGE.encode())
    assert r.status_code == 201
    multi, mixed, bad, nogeom = measurements(client, r.json()["id"])["features"]
    assert multi["feature_id"] == "multi" and multi["geometry_type"] == "MultiPolygon"
    assert multi["measurement"]["status"] == "COMPLETED"
    assert 2 * 1.2e6 * 0.9 < multi["measurement"]["value"] < 2 * 1.2e6 * 1.1  # two ~1.1km x 1.1km squares
    assert mixed["measurement"]["status"] == "UNSUPPORTED"
    assert bad["measurement"]["status"] == "FAILED" and bad["geometry"] is None
    assert nogeom["measurement"]["status"] == "FAILED"


def test_malformed_kml_is_400_and_recorded_as_failed(client):
    r = upload(client, "broken.kml", b"<kml><oops>")
    assert r.status_code == 400
    info = client.get(f"/api/files/{r.json()['id']}/").json()
    assert info["status"] == "FAILED" and info["error"]
    assert client.get(f"/api/files/{r.json()['id']}/measurements/").status_code == 409


def test_kml_with_entity_expansion_is_rejected(client):
    evil = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml><Placemark>&a;</Placemark></kml>'
    assert upload(client, "evil.kml", evil).status_code == 400


def test_shapefile_geographic_polygons(client, tmp_path):
    # A Shapefile holds a single geometry type, so lines/points are tested separately below.
    gdf = gpd.GeoDataFrame(
        {"name": ["plot", "plot2"], "area_ha": [1.5, None]},
        geometry=[
            Polygon([(77.5, 12.9), (77.501, 12.9), (77.501, 12.901), (77.5, 12.901)]),
            Polygon([(77.6, 12.9), (77.601, 12.9), (77.601, 12.901), (77.6, 12.901)]),
        ],
        crs="EPSG:4326",
    )
    r = upload(client, "survey.zip", zip_shapefile(tmp_path, gdf))
    assert r.status_code == 201, r.text
    assert r.json()["crs"] == "EPSG:4326"
    feats = measurements(client, r.json()["id"])["features"]
    assert len(feats) == 2
    assert 11_800 < feats[0]["measurement"]["value"] < 12_200
    assert feats[0]["properties"]["name"] == "plot"
    assert feats[1]["properties"]["area_ha"] is None  # NaN must become JSON null


def test_shapefile_lines_and_points(client, tmp_path):
    lines = gpd.GeoDataFrame(
        {"n": [1]}, geometry=[LineString([(77.5, 12.9), (77.502, 12.902)])], crs="EPSG:4326"
    )
    r = upload(client, "lines.zip", zip_shapefile(tmp_path, lines))
    assert r.status_code == 201, r.text
    f = measurements(client, r.json()["id"])["features"][0]
    assert f["measurement"]["type"] == "length" and 300 < f["measurement"]["value"] < 320

    points = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(77.5, 12.9)], crs="EPSG:4326")
    (tmp_path / "shp").mkdir(exist_ok=True)
    for old in (tmp_path / "shp").iterdir():
        old.unlink()
    r = upload(client, "points.zip", zip_shapefile(tmp_path, points))
    assert r.status_code == 201, r.text
    f = measurements(client, r.json()["id"])["features"][0]
    assert f["geometry_type"] == "Point" and f["measurement"]["status"] == "NOT_REQUIRED"


def test_shapefile_projected_crs_is_measured_in_metres(client, tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["sq"]},
        geometry=[Polygon([(500000, 1428000), (500100, 1428000), (500100, 1428100), (500000, 1428100)])],
        crs="EPSG:32643",
    )
    r = upload(client, "utm.zip", zip_shapefile(tmp_path, gdf))
    assert r.status_code == 201, r.text
    value = measurements(client, r.json()["id"])["features"][0]["measurement"]["value"]
    assert value == pytest.approx(10_000, rel=1e-4)


def test_shapefile_without_crs_requires_override(client, tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["p"]},
        geometry=[Polygon([(77.5, 12.9), (77.501, 12.9), (77.501, 12.901), (77.5, 12.901)])],
    )
    data = zip_shapefile(tmp_path, gdf)
    assert upload(client, "nocrs.zip", data).status_code == 400
    ok = upload(client, "nocrs.zip", data, crs="EPSG:4326")
    assert ok.status_code == 201, ok.text
    assert ok.json()["crs"] == "EPSG:4326"


def test_macosx_junk_is_ignored(client, tmp_path):
    gdf = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(77.5, 12.9)], crs="EPSG:4326")
    data = zip_shapefile(tmp_path, gdf, extra={"__MACOSX/._layer.shp": b"junk"})
    assert upload(client, "mac.zip", data).status_code == 201


def test_invalid_zip_and_missing_shp(client):
    assert upload(client, "bad.zip", b"not a zip").status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert upload(client, "empty.zip", buf.getvalue()).status_code == 400


def test_zip_slip_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.shp", "x")
    assert upload(client, "slip.zip", buf.getvalue()).status_code == 400
