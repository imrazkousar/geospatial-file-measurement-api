# Geospatial File Measurement API

A FastAPI backend that accepts a zipped ESRI Shapefile (`.zip`) or a KML (`.kml`), extracts every feature
(index/ID, geometry type, geometry, CRS, properties) and calculates **polygon area** and **line length in metres**
after reprojecting to a suitable projected CRS. Raw longitude/latitude degrees are never measured directly.

## Setup

Requires Python 3.10+ (GeoPandas/pyproj/shapely ship binary wheels, so no system GDAL install is needed).

```bash
git clone https://github.com/<your-username>/geospatial-file-measurement-api.git
cd geospatial-file-measurement-api

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # use requirements.txt for runtime only

uvicorn app.main:app --reload
```

- Interactive docs: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health/

Run the tests: `pytest`

Docker alternative: `docker build -t geo-api . && docker run -p 8000:8000 -v geo-data:/data geo-api`

| Env var | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | Where uploads and the SQLite DB are stored |
| `MAX_UPLOAD_BYTES` | 50 MiB | Max upload size (HTTP 413 above it) |
| `MAX_UNCOMPRESSED_BYTES` | 500 MiB | Max total size of a ZIP once extracted (zip-bomb guard) |

A sample KML is in `samples/sample.kml`; `python scripts/make_sample_shapefile.py` creates a sample Shapefile ZIP.

## API

### `POST /api/files/` — upload and process

`multipart/form-data`, field `file` (`.zip` containing one Shapefile, or `.kml`).
Optional form field `crs` (e.g. `EPSG:32643`) is used **only** if the Shapefile has no `.prj`.

```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@samples/sample.kml"
```

`201 Created`
```json
{
  "id": "a1b2c3d4e5f6",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-08T10:00:00+00:00",
  "error": null,
  "measurements_url": "/api/files/a1b2c3d4e5f6/measurements/"
}
```

Errors (all JSON `{"detail": ...}`): `400` wrong extension / empty file / invalid ZIP, KML or Shapefile / Shapefile with no CRS;
`413` too large; `422` unexpected processing failure. When the file was received but could not be processed, the body is
`{"id": "...", "status": "FAILED", "detail": "..."}` and the failure is retrievable via `GET /api/files/{id}/`.

### `GET /api/files/{id}/` — file information

```json
{ "id": "a1b2c3d4e5f6", "filename": "sample.kml", "feature_count": 3, "crs": "EPSG:4326",
  "status": "COMPLETED", "created_at": "2026-10-08T10:00:00+00:00", "error": null }
```
`status` is `COMPLETED` or `FAILED`. Unknown id → `404`.

### `GET /api/files/{id}/measurements/?limit=1000&offset=0` — per-feature measurements

```json
{
  "file_id": "a1b2c3d4e5f6",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "offset": 0,
  "limit": 1000,
  "summary": {
    "total_area_m2": 12023.1,
    "total_length_m": 310.4,
    "geometry_types": {"Polygon": 1, "LineString": 1, "Point": 1},
    "measurement_statuses": {"COMPLETED": 2, "NOT_REQUIRED": 1}
  },
  "features": [
    {
      "feature_index": 0,
      "feature_id": "0",
      "geometry_type": "Polygon",
      "geometry": {"type": "Polygon", "coordinates": [[[77.5, 12.9], [77.501, 12.9], [77.501, 12.901], [77.5, 12.901], [77.5, 12.9]]]},
      "crs": "EPSG:4326",
      "properties": {"name": "Sample Survey Polygon", "plot": "A-01"},
      "measurement": {"status": "COMPLETED", "type": "area", "value": 12023.1, "unit": "m²",
                      "crs": "EPSG:32643", "message": null}
    },
    {
      "feature_index": 2, "feature_id": "2", "geometry_type": "Point",
      "geometry": {"type": "Point", "coordinates": [77.5, 12.9]}, "crs": "EPSG:4326",
      "properties": {"name": "Sample Marker"},
      "measurement": {"status": "NOT_REQUIRED", "type": null, "value": null, "unit": null, "crs": null, "message": null}
    }
  ]
}
```
(Numbers are illustrative.) `measurement.status` is one of `COMPLETED`, `NOT_REQUIRED` (points),
`UNSUPPORTED` (empty / mixed / unknown geometry) or `FAILED` (bad geometry or un-projectable coordinates);
`message` explains non-trivial outcomes. A failed file returns `409`.

## Architecture

```
app/
├── main.py          HTTP layer: routes, upload streaming + size limit, error mapping
├── service.py       parse -> measure -> persist orchestration for one file
├── parsers.py       KML parser and Shapefile (zip) reader -> common RawFeature/CRS structure
├── measurements.py  projected-CRS selection and area/length calculation
├── db.py            SQLite persistence (files + features tables)
├── schemas.py       Pydantic response models (OpenAPI docs)
└── config.py        environment-based settings
tests/               API + unit tests (pytest)
```

**File-processing flow**
1. `POST /api/files/` validates the extension, streams the upload to disk (aborting above the size limit).
2. Parsing runs in a worker thread (CPU-bound, keeps the event loop free):
   - **KML**: parsed with `defusedxml` (blocks entity-expansion attacks). Point, LineString, Polygon (with holes) and
     MultiGeometry become Shapely geometries; `name`, `description` and `ExtendedData` become properties.
   - **ZIP**: path-traversal and uncompressed-size checks, extraction to a temp dir (`__MACOSX`/`._*` junk ignored),
     then GeoPandas reads the `.shp`; attribute values are made JSON-safe (NaN→null, dates→ISO strings).
3. A malformed *feature* (e.g. a LineString with one point) is recorded as `FAILED` without failing the whole file;
   a malformed *file* is stored with status `FAILED` and an error message.
4. File metadata and all features (with measurements) are stored in SQLite in one transaction.

**Measurement flow** (`measurements.measure`)
- Flatten the geometry into parts. Only points → `NOT_REQUIRED`; only polygons → area; only lines → length;
  empty/mixed/unknown → `UNSUPPORTED`. Multi-part geometries are supported (areas/lengths are summed).
- Reproject, then measure with Shapely in metres; area is returned in m², length in m. Results are rounded to 4 d.p.
- Invalid polygons (self-intersections) are still measured but flagged in `message`.

**CRS handling**
- KML is always WGS 84 (`EPSG:4326`). For Shapefiles the CRS is read from the `.prj`; the reported `crs` is the file's
  *source* CRS, and each measurement also reports the *projected* CRS used (`measurement.crs`).
- The geometry's bounding-box centre is computed in lon/lat and used to pick the **UTM zone** (northern `326xx`,
  southern `327xx`); beyond 84°N / 80°S it uses UPS (`32661`/`32761`). Geometry is transformed with `pyproj`
  (`always_xy=True`, so axis order is never ambiguous) and measured in that CRS. Z values are dropped before measuring.
- Shapefiles without a CRS are **rejected** with a clear message unless the client supplies `crs` — guessing would
  silently produce wrong numbers.

## Design decisions

| Decision | Why / alternatives considered |
|---|---|
| **FastAPI** | Typed request/response models and automatic OpenAPI docs. Django+DRF would be heavier for a small API with no auth/admin needs. |
| **UTM zone per feature** | Simple, metric, accurate to ~0.1% within a zone, and needs no external data. Alternatives: a per-feature *local equal-area* (LAEA) projection (exact areas but distorts lengths), or **geodesic** measurement via `pyproj.Geod` (most accurate, handles any extent — a good future option). |
| **Hand-written KML parser** | Avoids depending on GDAL's optional KML driver being compiled into the wheel, and lets bad Placemarks fail individually. Trade-off: it covers common constructs only (see limitations). |
| **Process on upload, store results** | Measurement requests are cheap and consistent. A background queue (Celery/RQ) is the next step for large files; the `status` field is already modelled for it. |
| **SQLite, stdlib `sqlite3`** | Zero-setup local run. PostGIS would be better for multi-user deployments and spatial queries. |
| **One Shapefile per ZIP** | Avoids ambiguous feature numbering/CRS across layers; multiple `.shp` files are rejected with a clear error. |
| **Failures are persisted** | A rejected-but-received file still has an id and error message that clients can query. |

## Learnings

- Why measuring on lon/lat degrees is wrong, and how UTM zones/projections trade off area vs. length distortion.
- `pyproj` axis-order pitfalls (`always_xy=True`) and the CRS-from-`.prj` workflow in GeoPandas.
- Real-world file hazards: zip-slip, zip bombs, XML entity expansion, macOS `__MACOSX` entries, NaN in attribute
  tables breaking JSON, and Shapefiles allowing only one geometry type.
- Designing for graceful degradation: per-feature statuses instead of failing the whole request.

## Future scope

- Background processing (Celery/RQ) with `PENDING/PROCESSING` statuses and polling or webhooks.
- Geodesic measurements (`pyproj.Geod`) as an option, and a client-selectable target CRS.
- More formats: KMZ, GeoJSON, GeoPackage; multi-layer files; `gx:Track`; 3D length.
- PostgreSQL/PostGIS storage, spatial queries, authentication, quotas/rate limiting, file retention/cleanup.
- Geometry repair (`make_valid`), unit selection (ha, acres, km, miles), CI pipeline, structured logging and metrics.

## Limitations

- KML coverage is Point/LineString/Polygon/MultiGeometry with ExtendedData; styles, NetworkLinks, `gx:` extensions and
  altitude are ignored.
- Features spanning multiple UTM zones (or crossing the antimeridian) are measured in a single zone, so accuracy
  degrades for very large geometries.
- Mixed-type geometry collections (e.g. polygon + line in one Placemark) are reported `UNSUPPORTED`.
- Uploads are processed synchronously; ZIP uncompressed-size checks rely on the sizes declared in the archive.
