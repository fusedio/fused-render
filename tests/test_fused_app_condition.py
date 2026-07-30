"""The folder-level `fused_app` template's condition.py gate (SPEC CT-12).

The gate runs on EVERY directory the user opens (it rides the universal `/`
key), so like the graph gate it must never enumerate the directory — the
probes are targeted isfile/stat calls plus one bounded read of the manifest.
"""
import contextlib
import importlib.util
import json
import os
from unittest import mock

import pytest

from fused_render.server import templates as server

CONDITION = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "fused_app", "condition.py")


def _load():
    spec = importlib.util.spec_from_file_location("fused_app_condition", CONDITION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@contextlib.contextmanager
def _no_enumeration():
    """Any directory enumeration inside a gate call is a test failure."""
    import glob as glob_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("the gate must never enumerate a directory (CT-12)")

    with mock.patch.object(os, "listdir", forbidden), \
            mock.patch.object(os, "scandir", forbidden), \
            mock.patch.object(os, "walk", forbidden), \
            mock.patch.object(glob_mod, "glob", forbidden), \
            mock.patch.object(glob_mod, "iglob", forbidden):
        yield


@pytest.fixture(scope="module")
def gate():
    main = _load()

    def call(path):
        with _no_enumeration():
            return main(path)

    return call


@pytest.fixture()
def app_dir(tmp_path):
    counter = [0]

    def make(manifest, entry_files=("index.html",)):
        counter[0] += 1
        d = tmp_path / f"app{counter[0]}"
        d.mkdir()
        if manifest is not None:
            body = manifest if isinstance(manifest, str) else json.dumps(manifest)
            (d / "fused_app.json").write_text(body, encoding="utf-8")
        for name in entry_files:
            (d / name).write_text("<html></html>", encoding="utf-8")
        return str(d)

    return make


def _manifest(entry="index.html", extra_pages=(), **top):
    pages = [{"path": "/", "file": entry}] + list(extra_pages)
    return {"fused_app": 1, "pages": pages, **top}


def test_valid_manifest_is_an_app(gate, app_dir):
    assert gate(app_dir(_manifest())) is True


def test_folder_without_manifest_is_not(gate, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    assert gate(str(d)) is False


def test_malformed_json_fails_closed(gate, app_dir):
    assert gate(app_dir("{not json")) is False


def test_non_object_manifest_fails_closed(gate, app_dir):
    assert gate(app_dir("[1, 2]")) is False


def test_missing_pages_fails_closed(gate, app_dir):
    assert gate(app_dir({"fused_app": 1, "title": "x"})) is False


def test_non_array_pages_fails_closed(gate, app_dir):
    assert gate(app_dir({"pages": {"path": "/", "file": "index.html"}})) is False


def test_no_root_route_fails_closed(gate, app_dir):
    # pages exist but none is routed at "/" — no entry page, not an app.
    assert gate(app_dir({"pages": [{"path": "/stats", "file": "index.html"}]})) is False


def test_non_string_entry_file_fails_closed(gate, app_dir):
    assert gate(app_dir({"pages": [{"path": "/", "file": 3}]})) is False


def test_dangling_entry_file_fails_closed(gate, app_dir):
    assert gate(app_dir({"pages": [{"path": "/", "file": "missing.html"}]})) is False


def test_escaping_entry_path_fails_closed(gate, app_dir, tmp_path):
    (tmp_path / "outside.html").write_text("<html></html>", encoding="utf-8")
    assert gate(app_dir(_manifest(entry="../outside.html"))) is False
    assert gate(app_dir(_manifest(entry="/etc/hosts"))) is False


def test_oversized_manifest_fails_closed(gate, app_dir):
    big = json.dumps(_manifest(pad="x" * (300 * 1024)))
    assert gate(app_dir(big)) is False


def test_nonexistent_path_fails_closed(gate, tmp_path):
    assert gate(str(tmp_path / "gone")) is False


# ------------------------------------------------------- resolution wiring

def test_app_dir_resolves_fused_app_mode(app_dir):
    # A directory with a valid manifest carries `fused_app` (gated, first)
    # ahead of the `_listing` sentinel via the universal `/` key.
    path = app_dir(_manifest())
    entries, error = server._templates_for(path, is_dir=True)
    assert error is None
    modes = [e["mode"] for e in entries]
    assert modes[:2] == ["fused_app", "_listing"]
    server._mark_conditions(entries)
    app_entry = entries[0]
    assert app_entry.get("conditional") is True
    cf = server._condition_file(app_entry["path"])
    allowed, err = server._run_condition(cf, path)
    assert (allowed, err) == (True, None)


def test_plain_dir_gate_denies(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    entries, _ = server._templates_for(str(d), is_dir=True)
    app_entry = next(e for e in entries if e["mode"] == "fused_app")
    cf = server._condition_file(app_entry["path"])
    allowed, err = server._run_condition(cf, str(d))
    assert allowed is False
    assert err is None
