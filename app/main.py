"""FastAPI application: upload geospatial files and retrieve per-feature measurements."""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import config, db, service
from .schemas import FeatureOut, FileInfo, MeasurementOut, MeasurementsResponse, Summary, UploadResponse

ALLOWED_SUFFIXES = {".zip", ".kml"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description="Upload Shapefile ZIPs or KML files and retrieve feature geometry and measurements.",
    version="1.1.0",
    lifespan=lifespan,
)


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f]", "", name)


def _get_file_or_404(file_id: str):
    row = db.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return row


@app.get("/health/", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/api/files/", status_code=201, response_model=UploadResponse, tags=["files"])
async def upload_file(
    file: UploadFile = File(..., description="A .zip containing a Shapefile, or a .kml file."),
    crs: str | None = Form(
        None,
        description="Optional CRS (e.g. 'EPSG:32643') used ONLY when a Shapefile has no .prj.",
    ),
):
    filename = _safe_filename(file.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Upload a .zip Shapefile archive or a .kml file.")

    file_id = uuid.uuid4().hex[:12]
    stored_path = config.upload_dir() / f"{file_id}{suffix}"
    db.init_db()  # ensures upload directory exists
    limit = config.max_upload_bytes()
    size = 0
    try:
        with stored_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413, detail=f"File exceeds the {limit // (1024 * 1024)} MB upload limit."
                    )
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    except HTTPException:
        stored_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        # Parsing/projection is CPU-bound; keep it off the event loop.
        record = await run_in_threadpool(
            service.process_file,
            file_id=file_id, filename=filename, stored_path=stored_path,
            suffix=suffix, crs_override=crs,
        )
    except service.ProcessingError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"id": exc.file_id, "status": service.STATUS_FAILED, "detail": exc.message},
        )
    return {**record, "measurements_url": f"/api/files/{file_id}/measurements/"}


@app.get("/api/files/{file_id}/", response_model=FileInfo, tags=["files"])
def get_file_info(file_id: str):
    row = _get_file_or_404(file_id)
    return dict(row)


@app.get("/api/files/{file_id}/measurements/", response_model=MeasurementsResponse, tags=["files"])
def get_measurements(
    file_id: str,
    limit: int = Query(1000, ge=1, le=10000, description="Max features to return."),
    offset: int = Query(0, ge=0, description="Number of features to skip."),
):
    row = _get_file_or_404(file_id)
    if row["status"] != service.STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail=f"File processing did not complete: {row['error']}")

    features = [
        FeatureOut(
            feature_index=r["feature_index"],
            feature_id=r["feature_id"],
            geometry_type=r["geometry_type"],
            geometry=json.loads(r["geometry_json"]) if r["geometry_json"] else None,
            crs=r["crs"],
            properties=json.loads(r["properties_json"]),
            measurement=MeasurementOut(
                status=r["measurement_status"], type=r["measurement_type"],
                value=r["measurement_value"], unit=r["measurement_unit"],
                crs=r["measurement_crs"], message=r["measurement_message"],
            ),
        )
        for r in db.list_features(file_id, limit, offset)
    ]
    return MeasurementsResponse(
        file_id=file_id, crs=row["crs"], feature_count=row["feature_count"],
        offset=offset, limit=limit, summary=Summary(**db.summarize(file_id)), features=features,
    )
