"""Tests for GET/PUT /api/home/layout (fused_render/shell/home_layout.py)."""
import json

from fastapi.testclient import TestClient

from fused_render.server import create_app

FUSED = {"X-Fused": "1"}


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return TestClient(create_app(start_dir=str(tmp_path))), home


def _layout():
    return {
        "version": 2,
        "widgets": [
            {"id": "s", "source": "search", "size": "4x1", "format": "bar"},
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards"},
            {"id": "b", "source": "folder", "size": "2x1", "format": "list", "folderId": "f1"},
        ],
    }


def test_get_absent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/home/layout")
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "layout": None}


def test_put_then_get_roundtrips(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    lay = _layout()
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert json.loads((home / "home_layout.json").read_text("utf-8")) == lay
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_put_requires_fused_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.put("/api/home/layout", json=_layout()).status_code == 403


def test_put_invalid_is_400(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    w = {"id": "a", "source": "apps", "size": "4x1", "format": "cards"}
    bad = [
        {"version": 6, "widgets": []},
        {"version": 0, "widgets": []},
        {"version": 1, "widgets": "x"},
        {"version": 1, "widgets": [1]},
        {"version": 1, "widgets": [{**w, "source": "nope"}]},
        {"version": 1, "widgets": [{**w, "size": "9x9"}]},
        {"version": 1, "widgets": [{**w, "format": 3}]},
        {"version": 1, "widgets": [w] * 49},
    ]
    for body in bad:
        assert client.put("/api/home/layout", json=body, headers=FUSED).status_code == 400, body
    assert client.get("/api/home/layout").json()["exists"] is False


def test_unknown_keys_dropped(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _layout()
    lay["extra"] = 1
    lay["widgets"][0]["junk"] = "x"
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]
    assert got == _layout()


def test_corrupt_file_reports_absent(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "home_layout.json").write_text("{ nope", encoding="utf-8")
    assert client.get("/api/home/layout").json() == {"exists": False, "layout": None}


def test_app_widget_roundtrips_app_path(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "x", "source": "app", "size": "2x2", "format": "live", "appPath": "/w/my app"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_app_widget_roundtrips_web_url(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "u", "source": "app", "size": "2x2", "format": "live", "appPath": "https://example.com/dash"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_tall_size_roundtrips(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {"version": 1, "widgets": [{"id": "t", "source": "tasks", "size": "1x2", "format": "list"}]}
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_build_widget_roundtrips(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {"version": 2, "widgets": [{"id": "b", "source": "build", "size": "4x1", "format": "bar"}]}
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_app_path_dropped_on_other_sources_and_when_oversized(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards", "appPath": "/w/x"},
            {"id": "b", "source": "app", "size": "2x2", "format": "live", "appPath": "p" * 4097},
            # Like a folder with no folderId: kept here, dropped by the client's normalize.
            {"id": "c", "source": "app", "size": "2x2", "format": "live"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert all("appPath" not in x for x in got)
    assert [x["id"] for x in got] == ["a", "b", "c"]


def test_apps_widget_roundtrips_sort(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards", "sort": "updated"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert got[0]["sort"] == "updated"


def test_sort_dropped_on_other_sources_and_unknown_values(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "r", "source": "recents", "size": "2x1", "format": "list", "sort": "name"},
            {"id": "a", "source": "apps", "size": "4x1", "format": "cards", "sort": "zzz"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert [x["id"] for x in got] == ["r", "a"]
    assert all("sort" not in x for x in got)


def test_tasks_widget_roundtrips_show(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "t", "source": "tasks", "size": "2x2", "format": "list", "show": "open"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert got[0]["show"] == "open"


def test_show_dropped_on_other_sources_and_unknown_values(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = {
        "version": 1,
        "widgets": [
            {"id": "r", "source": "recents", "size": "2x1", "format": "list", "show": "open"},
            {"id": "t", "source": "tasks", "size": "2x2", "format": "list", "show": "zzz"},
        ],
    }
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert [x["id"] for x in got] == ["r", "t"]
    assert all("show" not in x for x in got)


def test_old_version_document_is_kept_as_is(tmp_path, monkeypatch):
    # The server does not migrate: the client prepends the search widget to a
    # version-1 document and stamps 2 on its next write.
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    old = {"version": 1, "widgets": [{"id": "a", "source": "apps", "size": "4x1", "format": "cards"}]}
    (home / "home_layout.json").write_text(json.dumps(old), encoding="utf-8")
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": old}
    new = {"version": 2, "widgets": [{"id": "s", "source": "search", "size": "2x1", "format": "bar"}, *old["widgets"]]}
    assert client.put("/api/home/layout", json=new, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": new}


def _v3(*items):
    return {
        "version": 3,
        "widgets": [
            {"id": i, "source": "apps", "size": size, "format": "cards", "x": x, "y": y}
            for i, size, x, y in items
        ],
    }


def test_v3_coords_roundtrip(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    lay = _v3(("a", "2x2", 0, 0), ("b", "1x1", 3, 0), ("c", "4x1", 0, 5))
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert json.loads((home / "home_layout.json").read_text("utf-8")) == lay
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_v3_requires_integer_coords(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    base = _v3(("a", "1x1", 0, 0))["widgets"][0]
    for bad in ({"y": None}, {"x": True}, {"x": 1.5}, {"y": "2"}):
        lay = {"version": 3, "widgets": [{**base, **bad}]}
        assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400
    missing = {"version": 3, "widgets": [{k: v for k, v in base.items() if k != "x"}]}
    assert client.put("/api/home/layout", json=missing, headers=FUSED).status_code == 400


def test_v3_rejects_out_of_bounds(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for item in (("a", "2x1", 3, 0), ("a", "1x2", 0, 63), ("a", "1x1", -1, 0), ("a", "1x1", 0, -1), ("a", "1x1", 4, 0)):
        assert client.put("/api/home/layout", json=_v3(item), headers=FUSED).status_code == 400


def test_v3_rejects_overlap(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v3(("a", "2x2", 0, 0), ("b", "1x1", 1, 1))
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400
    ok = _v3(("a", "2x2", 0, 0), ("b", "1x1", 2, 1))
    assert client.put("/api/home/layout", json=ok, headers=FUSED).status_code == 200


def test_get_returns_an_overlapping_v3_layout_for_the_client_to_repair(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    lay = _v3(("a", "2x2", 0, 0), ("b", "1x1", 1, 1))
    (home / "home_layout.json").write_text(json.dumps(lay), encoding="utf-8")
    got = client.get("/api/home/layout").json()
    assert got["exists"] is True
    assert [w["id"] for w in got["layout"]["widgets"]] == ["a", "b"]


def test_get_drops_non_integer_v3_coords(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    lay = _v3(("a", "1x1", 0, 0))
    lay["widgets"][0]["x"] = 1.5
    (home / "home_layout.json").write_text(json.dumps(lay), encoding="utf-8")
    w = client.get("/api/home/layout").json()["layout"]["widgets"][0]
    assert "x" not in w and "y" not in w


def _v4(*items):
    lay = _v3(*items)
    lay["version"] = 4
    return lay


def test_v4_half_cell_roundtrip(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    lay = _v4(("a", "1x1", 1, 0), ("b", "2x2", 4, 3), ("c", "4x1", 0, 126))
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert json.loads((home / "home_layout.json").read_text("utf-8")) == lay
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_v4_rejects_out_of_bounds(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for item in (("a", "1x1", 7, 0), ("a", "1x1", 0, 127), ("a", "4x1", 1, 0), ("a", "1x1", -1, 0), ("a", "1x1", 0, -1)):
        assert client.put("/api/home/layout", json=_v4(item), headers=FUSED).status_code == 400
    # y=126 fits a 1x1's 2 unit rows exactly.
    assert client.put("/api/home/layout", json=_v4(("a", "1x1", 0, 126)), headers=FUSED).status_code == 200


def test_v4_rejects_half_cell_overlap(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v4(("a", "1x1", 1, 0), ("b", "1x1", 2, 0))  # units 1-2 vs 2-3
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400
    ok = _v4(("a", "1x1", 1, 0), ("b", "1x1", 3, 0))
    assert client.put("/api/home/layout", json=ok, headers=FUSED).status_code == 200


def test_v3_cell_bounds_still_apply(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    # x=7 is legal in v4 units but out of the 4-column v3 grid.
    assert client.put("/api/home/layout", json=_v3(("a", "1x1", 7, 0)), headers=FUSED).status_code == 400


def test_v4_explicit_footprint_roundtrips(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v4(("a", "1x1", 0, 0))
    lay["widgets"][0].update(cols=3, rows=2)
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json() == {"exists": True, "layout": lay}


def test_v4_explicit_footprint_bounds(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for cols, rows in ((9, 2), (1, 2), (3, 9), (3, 1)):
        lay = _v4(("a", "1x1", 0, 0))
        lay["widgets"][0].update(cols=cols, rows=rows)
        assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400


def test_v4_overlap_only_because_of_explicit_cols(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v4(("a", "1x1", 0, 0), ("b", "1x1", 4, 0))
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    lay["widgets"][0].update(cols=6, rows=2)
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400


def test_v4_cols_without_rows_is_dropped(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v4(("a", "1x1", 0, 0))
    lay["widgets"][0]["cols"] = 3
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    w = client.get("/api/home/layout").json()["layout"]["widgets"][0]
    assert "cols" not in w and "rows" not in w


def _v5(*items):
    """(id, source, size, x, y) items; format "bar" for the bare sources."""
    return {
        "version": 5,
        "widgets": [
            {"id": i, "source": src, "size": size, "format": "bar" if src in ("search", "build") else "cards", "x": x, "y": y}
            for i, src, size, x, y in items
        ],
    }


def test_versions_1_to_5_are_accepted(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for v in (1, 2, 3, 4, 5):
        lay = {"version": v, "widgets": []}
        assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200, v


def test_v5_fixed_rows_override_stored_rows(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v5(("s", "search", "4x1", 0, 0), ("b", "build", "4x1", 0, 1))
    lay["widgets"][0].update(cols=8, rows=2)
    lay["widgets"][1].update(cols=8, rows=5)
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    got = client.get("/api/home/layout").json()["layout"]["widgets"]
    assert (got[0]["cols"], got[0]["rows"]) == (8, 1)
    assert (got[1]["cols"], got[1]["rows"]) == (8, 4)


def test_v5_search_is_one_unit_tall_build_is_four(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    ok = _v5(("s", "search", "4x1", 0, 0), ("a", "apps", "4x1", 0, 1))
    assert client.put("/api/home/layout", json=ok, headers=FUSED).status_code == 200
    bad = _v5(("b", "build", "4x1", 0, 0), ("a", "apps", "4x1", 0, 3))
    assert client.put("/api/home/layout", json=bad, headers=FUSED).status_code == 400
    ok = _v5(("b", "build", "4x1", 0, 0), ("a", "apps", "4x1", 0, 4))
    assert client.put("/api/home/layout", json=ok, headers=FUSED).status_code == 200


def test_v5_card_widget_still_needs_two_unit_rows(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v5(("a", "apps", "4x1", 0, 0))
    lay["widgets"][0].update(cols=4, rows=1)
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 400


def test_v4_search_with_two_rows_is_kept_as_stored(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    lay = _v5(("s", "search", "4x1", 0, 0))
    lay["version"] = 4
    lay["widgets"][0].update(cols=8, rows=2)
    assert client.put("/api/home/layout", json=lay, headers=FUSED).status_code == 200
    assert client.get("/api/home/layout").json()["layout"] == lay
