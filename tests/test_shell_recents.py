"""Tests for GET/POST/PUT /api/recents (fused_render/shell/recents.py) — the
recently-opened-files store at ~/.fused-render/recents.json.

FUSED_RENDER_HOME is redirected to a tmp dir so no test touches the real home.
"""
import json
import time
from urllib.parse import quote

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import mounts as mounts_mod
from fused_render.shell import recents as recents_mod


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


def _view_url(path, search=""):
    # Encode each segment like the frontend's urlForFsPath (lib/router.ts).
    encoded = "/".join(quote(s, safe="") for s in str(path).lstrip("/").split("/"))
    return "/view/" + encoded + search


def _make_file(tmp_path, name="a.parquet"):
    f = tmp_path / name
    f.write_text("x", encoding="utf-8")
    return f


def test_get_defaults_when_absent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/recents")
    assert resp.status_code == 200
    assert resp.json() == {"collapsed": False, "entries": []}


def test_open_records_url_verbatim(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    url = _view_url(f, "?freq=2.4&_mode=code")
    resp = client.post("/api/recents/open", json={"url": url}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"recorded": True}

    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert saved["collapsed"] is False
    assert len(saved["entries"]) == 1
    # The exact url including its query string is stored verbatim (D20 posture).
    assert saved["entries"][0]["url"] == url
    assert "openedAt" in saved["entries"][0]

    assert client.get("/api/recents").json()["entries"][0]["url"] == url


def test_open_dedupes_by_fs_path_and_moves_to_top(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    a = _make_file(tmp_path, "a.csv")
    b = _make_file(tmp_path, "b.csv")
    client.post("/api/recents/open", json={"url": _view_url(a, "?x=1")}, headers=FUSED)
    client.post("/api/recents/open", json={"url": _view_url(b)}, headers=FUSED)
    # Re-open a with new params: moves to top, url replaced — not duplicated.
    client.post("/api/recents/open", json={"url": _view_url(a, "?x=2")}, headers=FUSED)

    entries = client.get("/api/recents").json()["entries"]
    assert [e["url"] for e in entries] == [_view_url(a, "?x=2"), _view_url(b)]


def test_open_rejects_non_file_urls(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    d = tmp_path / "sub"
    d.mkdir()
    for url in (
        _view_url(d),               # directory
        "/view/_panel?_layout=(x)",  # sentinel route
        "/view/_prefs",              # sentinel route
        _view_url(tmp_path / "gone.txt"),  # missing file
        "/embed/" + str(_make_file(tmp_path)).lstrip("/"),  # embed prefix
    ):
        resp = client.post("/api/recents/open", json={"url": url}, headers=FUSED)
        assert resp.status_code == 200
        assert resp.json() == {"recorded": False}
    assert not (home / "recents.json").exists()


def test_open_requires_url(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.post("/api/recents/open", json={}, headers=FUSED).status_code == 400


def test_open_stores_title_when_given(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    url = _view_url(f)
    resp = client.post(
        "/api/recents/open", json={"url": url, "title": "My DB app"}, headers=FUSED
    )
    assert resp.status_code == 200
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert saved["entries"][0]["title"] == "My DB app"
    assert client.get("/api/recents").json()["entries"][0]["title"] == "My DB app"


def test_open_without_title_omits_the_field(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert "title" not in saved["entries"][0]


def test_open_ignores_blank_or_non_string_title(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    client.post(
        "/api/recents/open", json={"url": _view_url(f), "title": "   "}, headers=FUSED
    )
    client.post(
        "/api/recents/open", json={"url": _view_url(f), "title": 42}, headers=FUSED
    )
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert "title" not in saved["entries"][0]


def test_open_re_record_updates_title(tmp_path, monkeypatch):
    # A re-record of the same fs path (e.g. once the iframe's <title> resolves
    # after the initial open) replaces the entry, so the title lands even
    # though the first record predates it.
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)
    client.post(
        "/api/recents/open",
        json={"url": _view_url(f), "title": "My DB app"},
        headers=FUSED,
    )
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert len(saved["entries"]) == 1
    assert saved["entries"][0]["title"] == "My DB app"


def test_get_hides_missing_files_without_deleting_them(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    keep = _make_file(tmp_path, "keep.csv")
    gone = _make_file(tmp_path, "gone.csv")
    client.post("/api/recents/open", json={"url": _view_url(gone)}, headers=FUSED)
    client.post("/api/recents/open", json={"url": _view_url(keep)}, headers=FUSED)
    gone.unlink()

    # Filtered from the response...
    entries = client.get("/api/recents").json()["entries"]
    assert [e["url"] for e in entries] == [_view_url(keep)]
    # ...but never deleted from disk (the file may come back).
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert len(saved["entries"]) == 2


def test_dedupe_replaces_dead_entry_for_same_path(tmp_path, monkeypatch):
    # Dedupe identity is the decoded fs path, existence-blind: an entry whose
    # file was deleted (and here recreated) must be REPLACED by a re-record of
    # the same path, not left wasting a cap slot beside the fresh entry.
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path, "reborn.csv")
    client.post("/api/recents/open", json={"url": _view_url(f, "?x=1")}, headers=FUSED)
    f.unlink()
    f = _make_file(tmp_path, "reborn.csv")
    client.post("/api/recents/open", json={"url": _view_url(f, "?x=2")}, headers=FUSED)

    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert [e["url"] for e in saved["entries"]] == [_view_url(f, "?x=2")]


def test_entries_capped_at_20(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    for i in range(25):
        f = _make_file(tmp_path, f"f{i}.txt")
        client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)
    saved = json.loads((home / "recents.json").read_text(encoding="utf-8"))
    assert len(saved["entries"]) == 20
    # Newest first: the last open is on top, the oldest five fell off.
    assert saved["entries"][0]["url"] == _view_url(tmp_path / "f24.txt")
    assert all(e["url"] != _view_url(tmp_path / "f4.txt") for e in saved["entries"])


def test_collapsed_roundtrip(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)

    resp = client.put("/api/recents/collapsed", json={"collapsed": True}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"collapsed": True}
    data = client.get("/api/recents").json()
    assert data["collapsed"] is True
    assert len(data["entries"]) == 1  # entries survive the collapse write

    client.put("/api/recents/collapsed", json={"collapsed": False}, headers=FUSED)
    assert client.get("/api/recents").json()["collapsed"] is False


def test_collapsed_must_be_boolean(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.put("/api/recents/collapsed", json={"collapsed": "yes"}, headers=FUSED)
    assert resp.status_code == 400


def test_writes_without_fused_header_are_rejected(tmp_path, monkeypatch):
    # D3 guard: a blind cross-origin write (no X-Fused) must not land.
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path)
    assert client.post("/api/recents/open", json={"url": _view_url(f)}).status_code == 403
    assert client.put("/api/recents/collapsed", json={"collapsed": True}).status_code == 403
    assert not (home / "recents.json").exists()


def test_get_is_bounded_when_existence_check_hangs(tmp_path, monkeypatch):
    # A stale entry sitting on a slow/hung mount must never stall the sidebar.
    # With the existence check hung for 10s each, a serial GET would take
    # 30s+; the bounded, fan-out GET must return well under the hang duration
    # AND keep the entries (fail open: a possibly-dead row beats a stalled
    # sidebar — only a check that COMPLETES False may filter).
    client, _ = _client(tmp_path, monkeypatch)
    urls = []
    for i in range(3):
        f = _make_file(tmp_path, f"hang{i}.parquet")
        url = _view_url(f)
        urls.append(url)
        client.post("/api/recents/open", json={"url": url}, headers=FUSED)

    def _hang(path):
        time.sleep(10)
        return False  # would filter if it ever completed within budget

    monkeypatch.setattr(recents_mod.os.path, "isfile", _hang)

    start = time.monotonic()
    resp = client.get("/api/recents")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert elapsed < 3.0, f"GET /api/recents took {elapsed:.1f}s — not bounded"
    # Fail open: every hung entry is still present.
    got = {e["url"] for e in resp.json()["entries"]}
    assert got == set(urls)


def test_get_checks_run_concurrently_not_serially(tmp_path, monkeypatch):
    # N entries each with a ~0.5s existence check must complete in ~one sleep
    # (concurrent fan-out), not N sleeps (serial). Five serial checks = 2.5s;
    # concurrent + shared budget stays well under that.
    client, _ = _client(tmp_path, monkeypatch)
    for i in range(5):
        f = _make_file(tmp_path, f"slow{i}.parquet")
        client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)

    def _slow(path):
        time.sleep(0.5)
        return True

    monkeypatch.setattr(recents_mod.os.path, "isfile", _slow)

    start = time.monotonic()
    resp = client.get("/api/recents")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 5
    assert elapsed < 1.5, f"GET took {elapsed:.1f}s — checks not concurrent"


def test_get_completed_false_filters_completed_true_shows(tmp_path, monkeypatch):
    # Fast-path regression: a check that COMPLETES False filters the entry; one
    # that completes True keeps it. (The completed-False path is what the
    # fail-open timeout path must NOT reach.)
    client, _ = _client(tmp_path, monkeypatch)
    keep = _make_file(tmp_path, "present.csv")
    gone = _make_file(tmp_path, "vanished.csv")
    client.post("/api/recents/open", json={"url": _view_url(gone)}, headers=FUSED)
    client.post("/api/recents/open", json={"url": _view_url(keep)}, headers=FUSED)
    gone.unlink()

    entries = client.get("/api/recents").json()["entries"]
    assert [e["url"] for e in entries] == [_view_url(keep)]


def test_get_mount_backed_paths_route_through_rc_not_isfile(tmp_path, monkeypatch):
    # Mount safety: a mount-backed recents path must be checked via the rclone
    # rc API (rc_stat_for), NEVER a kernel os.path.isfile — a raw GETATTR on a
    # hung NFS mount is the exact call that wedges it. The tri-state result
    # governs filtering: only a healthy-rcd-confirmed "missing" filters an
    # entry; "exists" keeps it, and "indeterminate" (rcd down / timeout / error)
    # keeps it too (fail open).
    client, _ = _client(tmp_path, monkeypatch)
    live = _make_file(tmp_path, "live_mount.parquet")
    indet = _make_file(tmp_path, "indet_mount.parquet")
    gone = _make_file(tmp_path, "gone_mount.parquet")
    client.post("/api/recents/open", json={"url": _view_url(live)}, headers=FUSED)
    client.post("/api/recents/open", json={"url": _view_url(indet)}, headers=FUSED)
    client.post("/api/recents/open", json={"url": _view_url(gone)}, headers=FUSED)

    monkeypatch.setattr(mounts_mod, "is_mount_backed", lambda p: True)

    def _no_isfile(path):
        raise AssertionError("os.path.isfile called on a mount-backed path")

    monkeypatch.setattr(recents_mod.os.path, "isfile", _no_isfile)

    def _stat(path):
        if path.endswith("live_mount.parquet"):
            return "exists"
        if path.endswith("gone_mount.parquet"):
            return "missing"  # healthy rcd, item null -> trustworthy negative
        return "indeterminate"  # rcd down / timeout / error

    monkeypatch.setattr(mounts_mod, "rc_stat_for", _stat)

    resp = client.get("/api/recents")
    assert resp.status_code == 200
    got = {e["url"] for e in resp.json()["entries"]}
    # exists + indeterminate kept; only the confirmed-missing entry filtered.
    assert got == {_view_url(live), _view_url(indet)}


def test_corrupt_file_reads_as_defaults(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "recents.json").write_text("{ not json", encoding="utf-8")
    assert client.get("/api/recents").json() == {"collapsed": False, "entries": []}
    # A write recovers the file.
    f = _make_file(tmp_path)
    client.post("/api/recents/open", json={"url": _view_url(f)}, headers=FUSED)
    assert len(client.get("/api/recents").json()["entries"]) == 1
