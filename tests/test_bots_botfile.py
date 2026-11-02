"""Opening a `.fusedbot` package (botfile.py, file_routes.py; docs/bots.md §17): a malformed index is a
refusal not a 500, re-install mode is bound to the very package the bot came from, the package's README.md /
icon.svg land in the new bot's folder, and the install/upload routes bound and pre-check what they accept.
_bots_conftest points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp dirs; nothing touches the network."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import http.client
import json
import os
import urllib.parse

import pytest

from fused_render.bots import paths as bpaths


def _app(folder: str, title: str) -> str:
    app = os.path.join(bpaths.apps_root(), folder)
    os.makedirs(app)
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as f:
        f.write(f'<!doctype html><html><head><meta name="fused-app"><title>{title}</title></head><body>hi</body></html>')
    return app


def _bundled(folder: str, name: str) -> dict:
    return {"folder": folder, "name": name, "app_id": None, "source": {"kind": "bundled"}}


def _second_machine(tmp_path, monkeypatch):
    """A fresh workspace for the importer (the exporter's apps and bots stay behind)."""
    from fused_render.bots import bot as bm

    ws2 = tmp_path / "ws2"
    (ws2 / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws2))
    reset_bots_caches()  # noqa: F405 — from _bots_conftest
    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)  # no greeting thread outliving the test
    return ws2


def _container(path, index: dict, members) -> str:
    from fused_render import appfile_container

    appfile_container.write(str(path), {"name": "Hand", **index}, members, kind="bot")
    return str(path)


# ------------------------------------------------------------------ index ---
@pytest.mark.parametrize("apps", [1, "x", {"folder": "a"}, True])
def test_non_list_apps_is_a_refusal_not_a_server_error(tmp_path, apps):
    from fused_render.bots import botfile

    path = _container(tmp_path / "bad.fusedbot", {"apps": apps}, [("bot.json", b'{"name": "Hand"}')])
    for light in (True, False):
        with pytest.raises(botfile.BotFileError, match="app list is not a list"):
            botfile.probe(path, light=light)
    with pytest.raises(botfile.BotFileError, match="app list is not a list"):
        botfile.install(path, apps=[], memory=False)


def test_missing_apps_is_no_apps(tmp_path):
    from fused_render.bots import botfile

    path = _container(tmp_path / "ok.fusedbot", {}, [("bot.json", b'{"name": "Hand"}')])
    assert botfile.probe(path, light=True)["apps"] == []


# ------------------------------------------------------------------ re-install ---
def test_reinstall_is_bound_to_the_package_not_a_same_second_twin(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, registry

    _app("app-a", "A")
    _app("app-b", "B")
    b = bm.create("Twin", "", "", "do things", greet=False)
    with b.lock:
        b.meta["apps"] = [_bundled("app-a", "A"), _bundled("app-b", "B")]
        b.save()
    # Two exports in the same second: identical bot.json and origin {bot_id, exported_at}, different app lists.
    monkeypatch.setattr(package, "_utc_now", lambda: "2026-11-02T12:00:00Z")
    pa = package.write(b.id, memory=False, apps=["app-a"], file_name="a", dest_dir=str(tmp_path / "out"))
    pb = package.write(b.id, memory=False, apps=["app-a", "app-b"], file_name="b", dest_dir=str(tmp_path / "out"))
    assert botfile._origin(botfile._index(pa)) == botfile._origin(botfile._index(pb))

    _second_machine(tmp_path, monkeypatch)
    out = botfile.install(pa, apps=[], memory=False)
    nb = registry.get(out["bot_id"])
    assert nb.meta["origin"]["package_sha"] == botfile._package_sha(botfile._index(pa))
    assert nb.meta["origin"]["package_sha"] != botfile._package_sha(botfile._index(pb))

    # A Retry naming the twin's path must not install the twin's apps into this bot.
    with pytest.raises(botfile.BotFileError, match="not the one this bot was installed from"):
        botfile.install(pb, apps=["app-b"], memory=False, bot_id=nb.id)
    assert not os.path.exists(os.path.join(bpaths.apps_root(), "app-b"))
    # The package it did come from still retries.
    again = botfile.install(pa, apps=["app-a"], memory=False, bot_id=nb.id)
    assert [(r["folder"], r["ok"]) for r in again["apps"]] == [("app-a", True)]
    assert "app-a" in (registry.get(nb.id).meta.get("trusted_apps") or [])


def test_reinstall_refuses_a_bot_without_the_package_digest(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, registry

    b = bm.create("Old", "", "", "do things", greet=False)
    path = package.write(b.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    nb = registry.get(botfile.install(path, apps=[], memory=False)["bot_id"])
    with nb.lock:
        nb.meta["origin"] = {k: v for k, v in nb.meta["origin"].items() if k != "package_sha"}
        nb.save()
    with pytest.raises(botfile.BotFileError, match="not the one this bot was installed from"):
        botfile.install(path, apps=[], memory=False, bot_id=nb.id)


# ------------------------------------------------------------------ README / icon ---
ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'


def test_readme_and_icon_survive_install_and_reexport(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package

    b = bm.create("Pretty", "", "", "do things", greet=False)
    folder = bpaths.bot_folder(b.id)
    with open(os.path.join(folder, "README.md"), "wb") as f:
        f.write(b"# Pretty\nIt does things.\n")
    with open(os.path.join(folder, "icon.svg"), "wb") as f:
        f.write(ICON)
    path = package.write(b.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))

    _second_machine(tmp_path, monkeypatch)
    out = botfile.install(path, apps=[], memory=False)
    assert out["warnings"] == []
    nf = bpaths.bot_folder(out["bot_id"])
    with open(os.path.join(nf, "README.md"), "rb") as f:
        assert f.read() == b"# Pretty\nIt does things.\n"
    with open(os.path.join(nf, "icon.svg"), "rb") as f:
        assert f.read() == ICON
    again = package.write(out["bot_id"], memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out2"))
    peek = botfile.probe(again, light=True)
    assert peek["readme"] and peek["icon"]


def test_oversize_icon_is_a_warning_not_a_failed_install(monkeypatch, tmp_path):
    from fused_render import appfile
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile

    big = b"<svg>" + b" " * appfile.MAX_ICON_BYTES + b"</svg>"
    path = _container(tmp_path / "big.fusedbot", {"origin": {"bot_id": "abc", "exported_at": "x"}},
                      [("bot.json", b'{"name": "Big"}'), ("README.md", b"hi"), ("icon.svg", big)])
    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)  # no greeting thread outliving the test
    out = botfile.install(path, apps=[], memory=False)
    assert out["ok"] and any(w.startswith("icon.svg: larger than") for w in out["warnings"])
    nf = bpaths.bot_folder(out["bot_id"])
    assert not os.path.exists(os.path.join(nf, "icon.svg"))
    with open(os.path.join(nf, "README.md"), "rb") as f:
        assert f.read() == b"hi"


# ------------------------------------------------------------------ routes ---
def test_install_body_over_the_cap_is_413(client):
    from fused_render.bots import file_routes

    body = json.dumps({"path": "/x.fusedbot", "pad": "x" * (file_routes.INSTALL_BODY_MAX_BYTES + 1)}).encode()
    status, _h, raw = client.post("/api/botfile/install", body, headers={"Content-Type": "application/json"}, raw=True)
    assert status == 413 and b"larger than" in raw


def test_install_body_over_the_cap_is_413_when_chunked(client):
    from fused_render.bots import file_routes

    u = urllib.parse.urlparse(client.base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    chunk = b"x" * 8192
    n = file_routes.INSTALL_BODY_MAX_BYTES // len(chunk) + 2
    try:
        conn.request("POST", "/api/botfile/install", body=iter([chunk] * n),
                     headers={"X-Fused": "1", "Content-Type": "application/json"}, encode_chunked=True)
        resp = conn.getresponse()
        assert resp.status == 413
    finally:
        conn.close()


def test_install_small_body_still_reaches_the_installer(client):
    status, _h, raw = client.post("/api/botfile/install", {"path": "relative.fusedbot", "apps": []})
    assert status == 400 and b"absolute" in raw


def test_upload_refuses_a_package_the_probe_could_not_open(client, tmp_path):
    from fused_render import appfetch

    no_json = _container(tmp_path / "nojson.fusedbot", {}, [("README.md", b"hi")])
    with open(no_json, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=nojson.fusedbot", f.read(), raw=True)
    assert status == 400 and json.loads(raw)["error"] == "this bot package has no bot.json"
    bad_json = _container(tmp_path / "bad.fusedbot", {}, [("bot.json", b"[1, 2]")])
    with open(bad_json, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=bad.fusedbot", f.read(), raw=True)
    assert status == 400 and json.loads(raw)["error"] == "bot.json is not an object"
    assert [n for n in os.listdir(appfetch.downloads_dir()) if not n.startswith(".")] == []
    assert [n for n in os.listdir(appfetch.downloads_dir()) if n.startswith(".upload-")] == []

    good = _container(tmp_path / "good.fusedbot", {}, [("bot.json", b'{"name": "Good"}')])
    with open(good, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=good.fusedbot", f.read(), raw=True)
    assert status == 200
    saved = json.loads(raw)["path"]
    assert os.path.dirname(saved) == appfetch.downloads_dir() and saved.endswith(".fusedbot")
