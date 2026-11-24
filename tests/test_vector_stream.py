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
    r = subprocess.run([VECTOR_PY, "-c", "import geopandas, pyogrio, pyarrow, truststore"],
                       capture_output=True, timeout=120, check=False)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _venv_ready(),
                                reason="the vector template venv is not built here")

_WRITE = r"""
import sys, geopandas as gpd, pyarrow.parquet as pq, pyogrio, shapely
d = sys.argv[1]
geoms = [shapely.Point(10 + i % 60 * 0.01, 50 + i // 60 * 0.01).buffer(0.004) for i in range(3000)]
gdf = gpd.GeoDataFrame({"id": range(3000), "name": [f"n{i % 13}" for i in range(3000)]},
                       geometry=geoms, crs=4326)
gdf.to_crs(32632).to_file(f"{d}/t.shp")
gdf.to_parquet(f"{d}/t.parquet", row_group_size=250)
t = pq.read_table(f"{d}/t.parquet")
with pq.ParquetWriter(f"{d}/footer.parquet", t.schema.remove_metadata()) as w:
    w.write_table(t, row_group_size=250)
    w.add_key_value_metadata({"geo": t.schema.metadata[b"geo"]})
gdf.drop(columns="geometry").assign(h3_index=1).to_parquet(f"{d}/h3.parquet")
pyogrio.write_dataframe(gdf.drop(columns="geometry").head(5), f"{d}/m.gpkg", layer="meta")
gdf.head(1000).to_file(f"{d}/m.gpkg", layer="roads", mode="a")
gdf.head(1000).to_crs(32632).to_file(f"{d}/two.gpkg", layer="roads")
gpd.GeoDataFrame({"id": range(500)}, geometry=gpd.points_from_xy(range(0, 500000, 1000), range(500)),
                 crs=3857).to_file(f"{d}/two.gpkg", layer="pts", mode="a")
gdf.head(600).to_file(f"{d}/six.geojson")
gpd.GeoDataFrame({"k": range(6)}, crs=4326, geometry=[
    shapely.Point(1, 1), shapely.box(0, 0, 1, 1), shapely.MultiPolygon([shapely.box(2, 2, 3, 3)]),
    shapely.GeometryCollection([shapely.Point(4, 4), shapely.LineString([(4, 4), (5, 5)])]), None,
    shapely.MultiLineString([[(0, 0), (1, 0)], [(0, 1), (1, 1)]])]).to_file(f"{d}/mixed.geojson")
gdf.to_parquet(f"{d}/arrow.parquet", row_group_size=250, geometry_encoding="geoarrow")
"""

_MAIN = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import vector_reader
print(json.dumps([vector_reader.main(**call) for call in json.loads(sys.argv[2])]))
"""

_STREAM = r"""
import base64, json, sys
import geopandas as gpd, numpy as np, pyogrio, shapely
sys.path.insert(0, sys.argv[1])
import vector_reader
vector_reader.CHUNK_COORDS = int(sys.argv[2])


def decode(part):
    ids = np.frombuffer(base64.b64decode(part["ids"]), "<u4")
    if part["kind"] == "geojson":
        return ids, shapely.from_geojson(part["geoms"])
    coords = np.frombuffer(base64.b64decode(part["coords"]), "<f8").reshape(-1, 2)
    offsets = tuple(np.frombuffer(base64.b64decode(o), "<i4").astype("int64") for o in part["offsets"])
    return ids, shapely.from_ragged_array(shapely.GeometryType[part["kind"].upper()], coords, offsets)


def truth(path):
    frames = ([gpd.read_parquet(path)] if path.endswith(".parquet") else
              [gpd.read_file(path, layer=n) for n, kind in pyogrio.list_layers(path) if kind])
    return np.concatenate([f.geometry.to_crs(4326).to_numpy() for f in frames])


out = []
for path, local in zip(sys.argv[3::2], sys.argv[4::2]):
    meta = vector_reader.main(path, chunk=700)
    chunks = [meta["first"]]
    while chunks[-1]["next"] is not None:
        chunks.append(vector_reader.main(path, op="chunk", start=chunks[-1]["next"], chunk=700))
    ids, geoms = map(np.concatenate, zip(*[decode(p) for c in chunks for p in c["parts"]]))
    out.append({"count": meta["count"], "chunks": len(chunks), "drawn": sorted(ids.tolist()),
                "same": bool(shapely.equals(geoms, truth(local)[ids]).all()),
                "kinds": sorted({p["kind"] for c in chunks for p in c["parts"]})})
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("vector")
    subprocess.run([VECTOR_PY, "-I", "-c", _WRITE, str(d)], check=True, timeout=300)
    return d


def _serve(bucket, files, *names):  # noqa: F811
    for name in names:
        bucket.objects[f"/v/{name}"] = (files / name).read_bytes()


def _run(script, *args):
    out = subprocess.run([VECTOR_PY, "-I", "-c", script, VECTOR_DIR, *map(str, args)],
                         check=True, timeout=300, capture_output=True, text=True).stdout
    return json.loads(out)


def _main(*calls):
    return _run(_MAIN, json.dumps(calls, default=str))


def _read(*paths):
    return _main(*({"file": p, "chunk": 600} for p in paths))


def test_a_streamed_shapefile_reads_its_sidecars_like_the_local_one(files, bucket):  # noqa: F811
    sidecars = sorted(p.name for p in files.glob("t.*") if p.suffix != ".parquet")
    assert {".shx", ".dbf", ".prj"} <= {os.path.splitext(n)[1] for n in sidecars}
    _serve(bucket, files, *sidecars)
    local, remote = _read(files / "t.shp", bucket.url("v/t.shp"))
    assert "error" not in local and remote == local
    assert local["crs"]["epsg"] == 32632 and [s["name"] for s in local["schema"]] == ["id", "name"]
    assert (local["count"], local["first"]["next"]) == (3000, 600)
    assert local["file_size"] == sum((files / n).stat().st_size for n in sidecars)


def test_a_geoparquet_chunk_streams_only_the_row_groups_it_draws(files, bucket):  # noqa: F811
    _serve(bucket, files, "t.parquet", "h3.parquet")
    local, remote, h3 = _read(files / "t.parquet", bucket.url("v/t.parquet"),
                              bucket.url("v/h3.parquet"))
    assert "error" not in local and remote == local
    assert (local["count"], local["first"]["next"], local["crs"]["epsg"]) == (3000, 600, 4326)
    size = len(bucket.objects["/v/t.parquet"])
    ranges = [rng for path, rng, _ in bucket.log if path == "/v/t.parquet"]
    assert ranges and all(r and r.startswith("bytes=") for r in ranges)
    assert bucket.sent["/v/t.parquet"] < size / 2
    assert "H3 mode" in h3["error"]


def test_geoparquet_with_geo_only_in_the_footer_streams_like_one_with_it_in_the_schema(files, bucket):  # noqa: F811
    _serve(bucket, files, "footer.parquet")
    local, remote = _read(files / "footer.parquet", bucket.url("v/footer.parquet"))
    assert "error" not in local and remote == local
    assert (local["count"], local["first"]["next"], local["crs"]["epsg"]) == (3000, 600, 4326)
    assert bucket.sent["/v/footer.parquet"] < len(bucket.objects["/v/footer.parquet"]) / 2


def test_a_vrt_is_refused_and_odd_parquet_columns_still_render(tmp_path):
    import struct

    import pyarrow as pa
    import pyarrow.parquet as pq

    (tmp_path / "secret.csv").write_text("token\nSECRET\n")
    (tmp_path / "evil.geojson").write_text(
        f"<OGRVRTDataSource><OGRVRTLayer name='x'><SrcDataSource>{tmp_path}/secret.csv"
        "</SrcDataSource><SrcLayer>secret</SrcLayer></OGRVRTLayer></OGRVRTDataSource>")
    geo = {"version": "1.0.0", "primary_column": "wkb", "columns": {"wkb": {"encoding": "WKB"}}}
    odd = pa.table({"wkb": [struct.pack("<BIdd", 1, 1, 1.0, 2.0)], "tags": [[1, 2]],
                    "at": pa.array([0], pa.timestamp("s", "UTC")), "geometry": ["attr"]})
    pq.write_table(odd.replace_schema_metadata({"geo": json.dumps(geo)}), tmp_path / "odd.parquet")
    pq.write_table(pa.table({"width3": [1]}), tmp_path / "plain.parquet")
    evil, odd, plain = _read(tmp_path / "evil.geojson", tmp_path / "odd.parquet", tmp_path / "plain.parquet")
    assert "SECRET" not in json.dumps(evil) and "not recognized" in evil["error"]
    assert odd["table"] == [{"tags": "[1, 2]", "at": "1970-01-01 00:00:00+00:00", "geometry": "attr"}]
    assert (odd["count"], len(odd["first"]["parts"])) == (1, 1)
    assert "H3" not in plain["error"]


@pytest.mark.integration
def test_https_streams_on_an_interpreter_without_ca_files(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    (out,) = _read("https://fused-asset.s3.amazonaws.com/bbox/dc_20km/0.parquet")
    assert "error" not in out and out["count"] == 1


def test_counts_skip_nonspatial_tables_and_a_chunk_that_reaches_the_end_ends_the_stream(files):
    gpkg, six = _read(files / "m.gpkg", files / "six.geojson")
    assert gpkg["layers"] == ["roads"] and "_layer" not in gpkg["table"][0]
    assert (gpkg["count"], gpkg["first"]["next"], six["count"], six["first"]["next"]) == (1000, 600, 600, None)


def test_geoarrow_encoded_geoparquet_maps_like_wkb_and_still_streams(files, bucket):  # noqa: F811
    _serve(bucket, files, "arrow.parquet")
    wkb, local, remote = _read(files / "t.parquet", files / "arrow.parquet", bucket.url("v/arrow.parquet"))
    assert "error" not in local and remote == local
    assert {**local, "file_size": 0} == {**wkb, "file_size": 0}
    assert bucket.sent["/v/arrow.parquet"] < len(bucket.objects["/v/arrow.parquet"]) / 2


def test_every_feature_streams_once_in_lon_lat_across_chunks_layers_and_crs(files, bucket):  # noqa: F811
    _serve(bucket, files, "t.parquet")
    args = [f for n in ("t.parquet", "t.shp", "two.gpkg", "mixed.geojson") for f in (files / n,) * 2]
    parquet, shp, two, mixed, remote = _run(_STREAM, 20_000, *args, bucket.url("v/t.parquet"), files / "t.parquet")
    for out in (parquet, shp, two):
        assert out["same"] and out["drawn"] == list(range(out["count"])) and out["chunks"] > out["count"] / 700
    assert (parquet["count"], two["count"]) == (3000, 1500) and remote == parquet
    assert two["kinds"] == ["Point", "Polygon"]
    assert mixed["same"] and mixed["drawn"] == [0, 1, 2, 3, 5]
    assert mixed["kinds"] == ["MultiLineString", "MultiPolygon", "Point", "geojson"]


def test_a_feature_over_the_coordinate_budget_is_still_sent_alone(files):
    (one,) = _run(_STREAM, 1, files / "six.geojson", files / "six.geojson")
    assert one["same"] and one["chunks"] == len(one["drawn"]) == 600


def test_attributes_are_read_by_the_id_the_map_drew(files):
    t, two = files / "t.parquet", files / "two.gpkg"
    assert _main({"file": t, "op": "row", "start": 1234}, {"file": t, "op": "column", "column": "name"},
                 {"file": t, "op": "column", "column": "missing"}, {"file": two, "op": "row", "start": 1003},
                 {"file": two, "op": "column", "column": "_layer"}, {"file": two, "op": "column", "column": "id"}) == [
        {"props": {"id": 1234, "name": "n12"}}, {"values": [f"n{i % 13}" for i in range(3000)]},
        {"values": [None] * 3000}, {"props": {"_layer": "pts", "id": 3}},
        {"values": ["roads"] * 1000 + ["pts"] * 500}, {"values": [*range(1000), *range(500)]}]
