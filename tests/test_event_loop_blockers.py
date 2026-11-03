"""Handlers that touch the disk or CPU must do it off the event loop.

A blocking call made straight from an `async def` route freezes every other
request (and every websocket) for as long as it runs. For each route that used
to do so, the slow call is wrapped here to record whether an event loop was
running in the thread that executed it: a worker thread (`asyncio.to_thread`)
has none, the loop's own thread does.
"""
import asyncio
import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fused_render import app_listing, appfile
from fused_render.server import create_app, image_convert
from fused_render.server.routers import fs_read

FUSED = {"X-Fused": "1"}
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_MARKER = '<meta name="fused-app" />'


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture()
def seen(monkeypatch):
    """`seen.wrap(obj, name, match=None)` -> list of on-loop flags, one per call."""
    class Seen:
        def wrap(self, obj, name, match=None):
            real = getattr(obj, name)
            flags: list[bool] = []

            def spy(*a, **kw):
                if match is None or match(*a, **kw):
                    flags.append(_on_loop())
                return real(*a, **kw)

            monkeypatch.setattr(obj, name, spy)
            return flags

    return Seen()


def _off_loop(flags):
    assert flags, "the slow call never ran"
    assert not any(flags), "blocking call ran on the event loop"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fs_read, "_thumb_dir", lambda: str(tmp_path / "thumbs"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _app(tmp_path, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "index.html").write_text(f"<html><head>{_MARKER}</head><body>hi</body></html>")
    return d


def test_appfile_export_zips_off_the_loop(env, tmp_path, seen):
    flags = seen.wrap(appfile, "export_app_file")
    r = env.post("/api/appfile/export", headers=FUSED, data={"path": str(_app(tmp_path))})
    assert r.status_code == 200, r.text
    _off_loop(flags)


def test_appfile_export_error_still_answers_and_cleans_up(env, tmp_path):
    r = env.post("/api/appfile/export", headers=FUSED, data={"path": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_set_preview_reads_and_writes_off_the_loop(env, tmp_path, seen):
    d = _app(tmp_path)
    entry = seen.wrap(app_listing, "app_entry")
    replace = seen.wrap(os, "replace", lambda src, dst, *a, **k: str(dst).endswith("preview.png"))
    r = env.post("/api/apps/preview", headers=FUSED, data={"path": str(d)},
                 files={"preview": ("preview.png", _PNG, "image/png")})
    assert r.status_code == 200, r.text
    _off_loop(entry)
    _off_loop(replace)


def test_fs_thumb_cache_probe_runs_off_the_loop(env, tmp_path, seen):
    src = tmp_path / "a.png"
    Image.new("RGB", (40, 30), (1, 2, 3)).save(src)
    flags = seen.wrap(os.path, "exists", lambda p, *a: str(p).endswith(".jpg"))
    r = env.get("/api/fs/thumb", params={"path": str(src)})
    assert r.status_code == 200, r.text
    _off_loop(flags)


def test_fs_walk_directory_check_runs_off_the_loop(env, tmp_path, seen):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("x")
    flags = seen.wrap(os.path, "isdir", lambda p, *a: str(p) == str(root))
    r = env.get("/api/fs/walk", params={"path": str(root)})
    assert r.status_code == 200, r.text
    _off_loop(flags)


def test_schedule_shot_stores_and_measures_off_the_loop(env, tmp_path, seen):
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), (9, 9, 9)).save(buf, "PNG")
    dims = seen.wrap(image_convert, "dimensions")
    makedirs = seen.wrap(os, "makedirs", lambda p, *a, **k: "shots" in str(p))
    r = env.post("/api/schedule/shot", headers=FUSED,
                 files={"file": ("a.png", io.BytesIO(buf.getvalue()), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "image"
    _off_loop(dims)
    _off_loop(makedirs)
