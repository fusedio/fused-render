"""Exported apps (fused_render/exported_apps.py): ``.fused`` files anywhere on
disk, discovered through the file index and listed on /apps under the virtual
"exported" tag, with recency recorded by POST /api/appfile/open into
~/.fused-render/appfile_recents.json (D392).

Like the git-repos tests, the index store is built by hand (a files parquet +
manifest), not by running a scan — these are listing/screening tests, not scan
tests.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import appfile, exported_apps, registered_apps
from fused_render.index.config import load_config
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    exported_apps._clear_cache()
    yield
    exported_apps._clear_cache()


@pytest.fixture(autouse=True)
def _quiet_freshness(monkeypatch):
    # The hub nudge spawns a real background freshness check; irrelevant here.
    monkeypatch.setattr(exported_apps, "_note_hub_opened", lambda cfg: None)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _write_files_index(paths):
    """A minimal index store holding exactly `paths` in one files partition —
    the same schema the compaction writes (index/store.schemas)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fused_render.index.store import schemas

    cfg = load_config()
    os.makedirs(cfg.files_dir, exist_ok=True)
    file_schema, _dirs = schemas(pa)
    rows = sorted(paths)
    part = "part-000000-00001.parquet"
    pq.write_table(
        pa.table({
            "path": rows,
            "dir": [p.rpartition("/")[0] for p in rows],
            "name": [p.rpartition("/")[2] for p in rows],
            "ext": [os.path.splitext(p)[1].lstrip(".").lower() for p in rows],
            "size": [1 for _ in rows],
            "mtime": [1000.0 + i for i, _ in enumerate(rows)],
            "depth": [p.count("/") for p in rows],
        }, schema=file_schema),
        os.path.join(cfg.files_dir, part),
    )
    with open(cfg.partitions_json, "w") as f:
        json.dump({"partitions": [{"file": part}], "rows": len(rows),
                   "updated": 0.0}, f)
    return cfg


def _fused_file(tmp_path, rel):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"zipbytes")
    return p


def _app_folder(root, name):
    d = root / name
    d.mkdir(parents=True)
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>hi</body></html>')
    return d


# ------------------------------------------------------------------ listing


def test_indexed_fused_files_become_exported_rows(tmp_path):
    f = _fused_file(tmp_path, "Downloads/sine.fused")
    _write_files_index([str(f)])
    rows = exported_apps.exported_apps()
    assert len(rows) == 1
    (row,) = rows
    assert row["tag"] == "exported"
    assert row["kind"] == "appfile"
    assert row["name"] == "sine"
    assert row["path"] == row["entry"] == str(f).replace(os.sep, "/")
    assert row["entry_html"] is None
    assert row["opened_at"] is None
    assert row["updated_at"] == 1000.0


def test_only_the_fused_extension_is_listed(tmp_path):
    f = _fused_file(tmp_path, "a.fused")
    other = tmp_path / "b.zip"
    other.write_bytes(b"x")
    _write_files_index([str(f), str(other)])
    assert [r["name"] for r in exported_apps.exported_apps()] == ["a"]


def test_vanished_dotted_and_no_index_rows_are_screened(tmp_path):
    gone = tmp_path / "gone.fused"           # indexed but deleted since
    dotted = _fused_file(tmp_path, ".hidden/x.fused")  # junk_path refuses
    kept = _fused_file(tmp_path, "kept.fused")
    _write_files_index([str(gone), str(dotted), str(kept)])
    assert [r["name"] for r in exported_apps.exported_apps()] == ["kept"]


def test_no_index_at_all_is_zero_rows_not_an_error(tmp_path):
    assert exported_apps.exported_apps() == []


def test_unreadable_index_is_zero_rows(tmp_path):
    f = _fused_file(tmp_path, "a.fused")
    cfg = _write_files_index([str(f)])
    part = json.load(open(cfg.partitions_json))["partitions"][0]["file"]
    with open(os.path.join(cfg.files_dir, part), "wb") as fh:
        fh.write(b"not parquet")
    assert exported_apps.exported_apps() == []


def test_recents_union_lists_a_file_the_index_has_not_seen(tmp_path):
    _write_files_index([])
    fresh = _fused_file(tmp_path, "Desktop/new.fused")
    assert exported_apps.record_open(str(fresh))
    rows = exported_apps.exported_apps()
    assert [r["name"] for r in rows] == ["new"]
    assert rows[0]["opened_at"] is not None
    assert rows[0]["updated_at"] == pytest.approx(os.path.getmtime(fresh))


def test_index_and_recents_dedupe_to_one_row_with_opened_at(tmp_path):
    f = _fused_file(tmp_path, "one.fused")
    _write_files_index([str(f)])
    assert exported_apps.record_open(str(f))
    exported_apps._clear_cache()
    rows = exported_apps.exported_apps()
    assert len(rows) == 1
    assert rows[0]["opened_at"] is not None


def test_query_result_is_ttl_cached(tmp_path):
    f = _fused_file(tmp_path, "a.fused")
    _write_files_index([str(f)])
    assert len(exported_apps.exported_apps()) == 1
    b = _fused_file(tmp_path, "b.fused")
    _write_files_index([str(f), str(b)])
    # Within the TTL the old result still answers; a cleared cache sees b.
    assert len(exported_apps.exported_apps()) == 1
    exported_apps._clear_cache()
    assert len(exported_apps.exported_apps()) == 2


# ------------------------------------------------------------- recents store


def test_record_open_refuses_non_fused_relative_and_missing(tmp_path):
    assert not exported_apps.record_open("relative.fused")
    assert not exported_apps.record_open(str(tmp_path / "missing.fused"))
    notzip = tmp_path / "app.html"
    notzip.write_text("x")
    assert not exported_apps.record_open(str(notzip))


def test_record_open_dedupes_and_refreshes(tmp_path):
    f = _fused_file(tmp_path, "a.fused")
    assert exported_apps.record_open(str(f))
    first = exported_apps.read_recents()[0]["openedAt"]
    assert exported_apps.record_open(str(f))
    entries = exported_apps.read_recents()
    assert len(entries) == 1
    assert entries[0]["openedAt"] >= first


def test_record_open_caps_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(exported_apps, "APPFILE_RECENTS_CAP", 3)
    for i in range(5):
        exported_apps.record_open(str(_fused_file(tmp_path, f"f{i}.fused")))
    assert len(exported_apps.read_recents()) == 3


# --------------------------------------------------- /api/apps + open route


def test_api_apps_serves_exported_rows_and_survives_no_index(client, tmp_path):
    f = _fused_file(tmp_path, "x.fused")
    _write_files_index([str(f)])
    tags = {a["tag"] for a in client.get("/api/apps").json()["apps"]}
    assert "exported" in tags


def test_api_apps_without_index_still_answers(client):
    assert client.get("/api/apps").json()["apps"] == []


def test_appfile_open_records_recency_on_the_source_file(client, tmp_path):
    app_dir = _app_folder(tmp_path, "demo")
    out = tmp_path / "demo.fused"
    appfile.export_app_file(str(app_dir), str(out))
    r = client.post("/api/appfile/open", json={"file": str(out)},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    assert [e["path"] for e in exported_apps.read_recents()] == [str(out)]


def test_home_row_includes_opened_exported_apps(client, tmp_path):
    app_dir = _app_folder(tmp_path, "homer")
    out = tmp_path / "homer.fused"
    appfile.export_app_file(str(app_dir), str(out))
    client.post("/api/appfile/open", json={"file": str(out)},
                headers={"X-Fused": "1"})
    apps = client.get("/api/apps/home").json()["apps"]
    mine = [a for a in apps if a.get("kind") == "appfile"]
    assert [a["name"] for a in mine] == ["homer"]
    assert mine[0]["opened_at"] is not None


# ----------------------------------------------------------- preview member

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


def test_export_bakes_a_capture_only_when_no_authored_preview(tmp_path):
    a = _app_folder(tmp_path / "one", "a")
    out_a = tmp_path / "a.fused"
    appfile.export_app_file(str(a), str(out_a), preview_bytes=PNG)
    assert appfile.read_preview(str(out_a)) == PNG

    b = _app_folder(tmp_path / "two", "b")
    (b / "preview.png").write_bytes(PNG + b"authored")
    out_b = tmp_path / "b.fused"
    appfile.export_app_file(str(b), str(out_b), preview_bytes=PNG)
    assert appfile.read_preview(str(out_b)) == PNG + b"authored"


def test_export_refuses_a_non_png_capture(tmp_path):
    a = _app_folder(tmp_path, "a")
    with pytest.raises(appfile.AppFileError):
        appfile.export_app_file(str(a), str(tmp_path / "a.fused"),
                                preview_bytes=b"GIF89a nope")


def test_preview_route_serves_the_member_or_404s(client, tmp_path):
    a = _app_folder(tmp_path / "one", "a")
    out = tmp_path / "a.fused"
    appfile.export_app_file(str(a), str(out), preview_bytes=PNG)
    r = client.get("/api/appfile/preview", params={"path": str(out)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG

    bare = _app_folder(tmp_path / "two", "bare")
    out2 = tmp_path / "bare.fused"
    appfile.export_app_file(str(bare), str(out2))
    assert client.get("/api/appfile/preview",
                      params={"path": str(out2)}).status_code == 404
    assert client.get("/api/appfile/preview",
                      params={"path": "relative.fused"}).status_code == 400


def test_post_export_carries_the_capture_into_the_download(client, tmp_path):
    a = _app_folder(tmp_path, "posted")
    r = client.post("/api/appfile/export", data={"path": str(a)},
                    files={"preview": ("preview.png", PNG, "image/png")},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    out = tmp_path / "posted.fused"
    out.write_bytes(r.content)
    assert appfile.read_preview(str(out)) == PNG
    # The guard holds: no X-Fused, no export.
    assert client.post("/api/appfile/export",
                       data={"path": str(a)}).status_code == 403


# ------------------------------------- extract-dir double-listing suppression


def test_registered_apps_refuses_the_extract_cache(tmp_path, workspace):
    inside = os.path.join(appfile.appfiles_root(), "demo-abc123", "files")
    os.makedirs(inside)
    (open(os.path.join(inside, "index.html"), "w")).write(
        '<html><head><meta name="fused-app" /></head></html>')
    assert not registered_apps.record_open(inside)
    # A historical entry written by an older build is filtered on read too.
    registered_apps.write_entries([
        {"path": inside, "openedAt": "2026-01-01T00:00:00+00:00"}])
    assert registered_apps.read_entries() == []
