import json
import os
import subprocess

import pytest
from _range_bucket import bucket  # noqa: F401

import fused_render
from fused_render import projectenv

VECTOR_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "vector")
VECTOR_PY = projectenv.interpreter_for(VECTOR_DIR)


def _venv_ready() -> bool:
    if not os.path.exists(VECTOR_PY):
        return False
    r = subprocess.run([VECTOR_PY, "-c", "import geopandas, pyogrio, pyarrow"],
                       capture_output=True, timeout=120, check=False)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _venv_ready(),
                                reason="the vector template venv is not built here")

_WRITE = r"""
import sys, geopandas as gpd, shapely
d = sys.argv[1]
geoms = [shapely.Point(10 + i % 60 * 0.01, 50 + i // 60 * 0.01).buffer(0.004) for i in range(3000)]
gdf = gpd.GeoDataFrame({"id": range(3000), "name": [f"n{i % 13}" for i in range(3000)]},
                       geometry=geoms, crs=4326)
gdf.to_crs(32632).to_file(f"{d}/t.shp")
gdf.to_parquet(f"{d}/t.parquet", row_group_size=250)
gdf.drop(columns="geometry").assign(h3_index=1).to_parquet(f"{d}/h3.parquet")
"""

_READ = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import vector_reader
print(json.dumps([vector_reader.main(f, max_features=600) for f in sys.argv[2:]]))
"""


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("vector")
    subprocess.run([VECTOR_PY, "-I", "-c", _WRITE, str(d)], check=True, timeout=300)
    return d


def _serve(bucket, files, *names):  # noqa: F811
    for name in names:
        bucket.objects[f"/v/{name}"] = (files / name).read_bytes()


def _read(*paths):
    out = subprocess.run([VECTOR_PY, "-I", "-c", _READ, VECTOR_DIR, *map(str, paths)],
                         check=True, timeout=300, capture_output=True, text=True).stdout
    return json.loads(out)


def test_a_streamed_shapefile_reads_its_sidecars_like_the_local_one(files, bucket):  # noqa: F811
    sidecars = sorted(p.name for p in files.glob("t.*") if p.suffix != ".parquet")
    assert {".shx", ".dbf", ".prj"} <= {os.path.splitext(n)[1] for n in sidecars}
    _serve(bucket, files, *sidecars)
    local, remote = _read(files / "t.shp", bucket.url("v/t.shp"))
    assert "error" not in local and remote == local
    assert local["crs"]["epsg"] == 32632 and [s["name"] for s in local["schema"]] == ["id", "name"]
    assert local["count"] == 3000 and local["shown"] == 600
    assert local["file_size"] == sum((files / n).stat().st_size for n in sidecars)


def test_geoparquet_streams_only_the_row_groups_it_shows(files, bucket):  # noqa: F811
    _serve(bucket, files, "t.parquet", "h3.parquet")
    local, remote, h3 = _read(files / "t.parquet", bucket.url("v/t.parquet"),
                              bucket.url("v/h3.parquet"))
    assert "error" not in local and remote == local
    assert (local["count"], local["shown"], local["crs"]["epsg"]) == (3000, 600, 4326)
    size = len(bucket.objects["/v/t.parquet"])
    ranges = [rng for path, rng, _ in bucket.log if path == "/v/t.parquet"]
    assert ranges and all(r and r.startswith("bytes=") for r in ranges)
    assert bucket.sent["/v/t.parquet"] < size / 2
    assert "H3 mode" in h3["error"]


def test_a_vrt_is_refused_and_odd_parquet_columns_still_render(tmp_path):
    import struct

    import pyarrow as pa
    import pyarrow.parquet as pq

    (tmp_path / "secret.csv").write_text("token\nSECRET\n")
    (tmp_path / "evil.geojson").write_text(
        f"<OGRVRTDataSource><OGRVRTLayer name='x'><SrcDataSource>{tmp_path}/secret.csv"
        "</SrcDataSource><SrcLayer>secret</SrcLayer></OGRVRTLayer></OGRVRTDataSource>")
    geo = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": {"encoding": "WKB"}}}
    odd = pa.table({"geometry": [struct.pack("<BIdd", 1, 1, 1.0, 2.0)], "tags": [[1, 2]],
                    "at": pa.array([0], pa.timestamp("s", "UTC"))})
    pq.write_table(odd.replace_schema_metadata({"geo": json.dumps(geo)}), tmp_path / "odd.parquet")
    pq.write_table(pa.table({"width3": [1]}), tmp_path / "plain.parquet")
    evil, odd, plain = _read(tmp_path / "evil.geojson", tmp_path / "odd.parquet", tmp_path / "plain.parquet")
    assert "SECRET" not in json.dumps(evil) and "error" in evil
    assert odd["count"] == 1 and odd["table"][0]["tags"] == "[1 2]"
    assert "H3" not in plain["error"]
