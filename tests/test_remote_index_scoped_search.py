"""Folder-scoped search over the `fused-asset` bucket index, and the
launcher's bucket rows.

- `remote_index.rank(..., prefix=)` — the S3 folder view's search box: hits
  only from that folder's subtree, keys still whole bucket keys.
- GET /api/remote-index/rank?under= — the same, through the route.
- `launcher.remote_results` / `remote_page_for` — the launcher's "Fused
  assets" rows and the explorer page an `s3` row opens.

The store is built for real from a small local parquet listing
(`FUSED_RENDER_REMOTE_INDEX_URL` may be a local path), in a throwaway home.
"""
from __future__ import annotations

import datetime as dt

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("duckdb")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fused_render import launcher, remote_index  # noqa: E402
from fused_render.server.routers import remote_index as remote_index_router  # noqa: E402

KEYS = [
    "agriculture_cdl_crops/CDL_crop_name.parquet",
    "agriculture_cdl_crops/notes/crop_codes.csv",
    "census/crop_survey.parquet",
    "census/tracts.parquet",
]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    listing = tmp_path / "listing.parquet"
    when = dt.datetime(2026, 11, 3, 17, 37, tzinfo=dt.timezone.utc)
    table = pa.table(
        {
            "key": sorted(KEYS),
            "size": [846_000 + i for i in range(len(KEYS))],
            "last_modified": pa.array([when] * len(KEYS), pa.timestamp("ms", tz="UTC")),
        }
    ).replace_schema_metadata({"schema_version": remote_index.SCHEMA_VERSION})
    pq.write_table(table, listing)
    monkeypatch.setenv(remote_index.ENV_URL, str(listing))
    state = remote_index._ingest("test")
    assert not state.get("error"), state
    # Nothing in this file may kick an ingest from a request path.
    monkeypatch.setattr(remote_index, "retry_if_needed", lambda: None)


def _keys(out: dict) -> set[str]:
    return {h["key"] for h in out["hits"]}


def test_scoped_rank_stays_inside_the_folder(store):
    out = remote_index.rank("crop", 50, True, prefix="agriculture_cdl_crops/")
    assert out["ready"] is True
    keys = _keys(out)
    # The census hit for "crop" is outside the folder.
    assert "census/crop_survey.parquet" not in keys
    assert {"agriculture_cdl_crops/CDL_crop_name.parquet",
            "agriculture_cdl_crops/notes/crop_codes.csv"} <= keys
    for h in out["hits"]:
        assert h["url"] == "s3://fused-asset/" + h["key"]


def test_scoped_rank_matches_below_the_folder_only(store):
    # "agriculture" names the folder itself, which is not inside its own subtree.
    out = remote_index.rank("agriculture", 50, True, prefix="agriculture_cdl_crops/")
    assert out["ready"] is True and out["hits"] == []


def test_scoped_rank_folder_hits_end_in_slash(store):
    out = remote_index.rank("notes", 50, True, prefix="agriculture_cdl_crops/")
    assert out["hits"][0] == {
        "url": "s3://fused-asset/agriculture_cdl_crops/notes/",
        "key": "agriculture_cdl_crops/notes/",
        "is_dir": True, "size": None, "mtime": None,
    }


def test_scoped_rank_unknown_folder_is_ready_and_empty(store):
    out = remote_index.rank("crop", 50, True, prefix="no_such_folder/")
    assert out["ready"] is True and out["hits"] == []


def test_scoped_rank_speaks_the_local_query_grammar(store):
    # A glob, at any depth below the folder (resolve_query's `**/` prefix).
    out = remote_index.rank("*.csv", 50, True, prefix="agriculture_cdl_crops/")
    assert out["mode"] == "glob"
    assert _keys(out) == {"agriculture_cdl_crops/notes/crop_codes.csv"}
    # A leading "/" pins the pattern to the folder itself (depth 1).
    out = remote_index.rank("/*.csv", 50, True, prefix="agriculture_cdl_crops/")
    assert _keys(out) == set()
    out = remote_index.rank("/*.parquet", 50, True, prefix="agriculture_cdl_crops/")
    assert _keys(out) == {"agriculture_cdl_crops/CDL_crop_name.parquet"}
    # Words split by whitespace match in order, even across a folder.
    out = remote_index.rank("notes codes", 50, True, prefix="agriculture_cdl_crops/")
    assert "agriculture_cdl_crops/notes/crop_codes.csv" in _keys(out)
    # Substring mode says so, with the pattern it ran.
    out = remote_index.rank("crop", 50, True, prefix="census/")
    assert (out["mode"], out["pattern"]) == ("substring", "crop")


def test_scoped_rank_at_the_bucket_root(store):
    # prefix "" is the bucket's root folder: a plain key search, no bucket hit.
    out = remote_index.rank("fused-asset", 50, True, prefix="")
    assert out["hits"] == []
    assert "census/tracts.parquet" in _keys(remote_index.rank("tracts", 50, True, prefix=""))


def test_scoped_rank_query_leaving_the_folder_answers_nothing(store):
    out = remote_index.rank("../census/*", 50, True, prefix="agriculture_cdl_crops/")
    assert out["ready"] is True and out["hits"] == []


def test_whole_bucket_rank_unchanged(store):
    out = remote_index.rank("crop", 50, True)
    assert "census/crop_survey.parquet" in _keys(out)
    # The bucket's own name still finds the bucket.
    assert remote_index.rank("fused-asset", 5, True)["hits"][0]["key"] == ""


def test_rank_route_under(store, monkeypatch):
    monkeypatch.setattr(remote_index_router, "canvases_enabled", lambda: True)
    app = FastAPI()
    app.include_router(remote_index_router.router)
    client = TestClient(app)
    r = client.get("/api/remote-index/rank",
                   params={"q": "crop", "under": "s3://fused-asset/census/"})
    assert r.status_code == 200
    assert {h["key"] for h in r.json()["hits"]} == {"census/crop_survey.parquet"}
    # A prefix spelled without its trailing "/" is the same folder.
    r = client.get("/api/remote-index/rank",
                   params={"q": "crop", "under": "s3://fused-asset/census"})
    assert {h["key"] for h in r.json()["hits"]} == {"census/crop_survey.parquet"}
    r = client.get("/api/remote-index/rank", params={"q": "tracts", "under": "s3://fused-asset/"})
    assert {h["key"] for h in r.json()["hits"]} == {"census/tracts.parquet"}
    r = client.get("/api/remote-index/rank", params={"q": "crop", "under": "s3://other-bucket/"})
    assert r.status_code == 400 and r.json()["code"] == "unsupported_bucket"
    r = client.get("/api/remote-index/rank", params={"q": "crop", "under": "census/"})
    assert r.status_code == 400 and r.json()["code"] == "bad_url"


def test_launcher_remote_rows(store, monkeypatch):
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    rows = launcher.remote_results("tracts")
    assert rows == [{
        "path": "s3://fused-asset/census/tracts.parquet",
        "url": "/explorer/view/s3%3A/fused-asset/census/tracts.parquet",
        "name": "tracts.parquet", "title": "tracts.parquet",
        "kind": "s3", "is_dir": False,
        "pinned": False, "recent": False, "running": False, "icon": None,
    }]
    assert len(launcher.remote_results("crop")) <= launcher.REMOTE_RESULTS
    # The bucket row is named after the bucket.
    bucket = launcher.remote_results("fused-asset")[0]
    assert bucket["name"] == "fused-asset" and bucket["is_dir"] is True
    assert launcher.remote_results("") == []


def test_launcher_remote_rows_off_without_canvases(store, monkeypatch):
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: False)
    assert launcher.remote_results("tracts") == []


def test_launcher_remote_rows_never_raise(monkeypatch):
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(remote_index, "retry_if_needed", lambda: None)
    monkeypatch.setattr(remote_index, "rank", boom)
    assert launcher.remote_results("x") == []


def test_remote_page_for():
    assert launcher.remote_page_for("s3://fused-asset/") == "/explorer/view/s3%3A/fused-asset"
    assert (launcher.remote_page_for("s3://fused-asset/a b/c#d.csv")
            == "/explorer/view/s3%3A/fused-asset/a%20b/c%23d.csv")
    assert launcher.remote_page_for("/Users/me/x") is None
