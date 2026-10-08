"""Create samples/sample_shapefile.zip (requires geopandas). Run from the repo root."""
import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

out = Path("samples")
out.mkdir(exist_ok=True)
gdf = gpd.GeoDataFrame(
    {"name": ["Survey plot", "Road", "Marker"]},
    geometry=[
        Polygon([(77.5, 12.9), (77.501, 12.9), (77.501, 12.901), (77.5, 12.901)]),
        LineString([(77.5, 12.9), (77.502, 12.902)]),
        Point(77.5, 12.9),
    ],
    crs="EPSG:4326",
)
with tempfile.TemporaryDirectory() as tmp:
    gdf.to_file(Path(tmp) / "sample.shp")
    shutil.make_archive(str(out / "sample_shapefile"), "zip", tmp)
print("wrote samples/sample_shapefile.zip")
