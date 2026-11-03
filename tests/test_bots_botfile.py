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


def test_upload_runs_the_probes_checks_apps_list_and_skill_members(client, tmp_path, monkeypatch):
    from fused_render import appfetch, appfile_container
    from fused_render.bots import botfile

    def upload(p):
        with open(p, "rb") as f:
            return client.post("/api/botfile/upload?name=x.fusedbot", f.read(), raw=True)

    bad_apps = _container(tmp_path / "apps.fusedbot", {"apps": 1}, [("bot.json", b'{"name": "Hand"}')])
    status, _h, raw = upload(bad_apps)
    assert status == 400 and json.loads(raw)["error"] == "this bot package's app list is not a list"

    # A playbook whose index hash does not match its bytes: the full probe refuses it, so the upload does too.
    real = appfile_container._put_member

    def lying(data, rel, src, offset):
        entry = real(data, rel, src, offset)
        return {**entry, "sha256": "0" * 64} if rel.startswith("skills/") else entry

    monkeypatch.setattr(appfile_container, "_put_member", lying)
    tampered = _container(tmp_path / "tampered.fusedbot", {},
                          [("bot.json", b'{"name": "Hand"}'), ("skills/greet.md", b"# Greet\nSay hi.\n")])
    monkeypatch.setattr(appfile_container, "_put_member", real)
    with pytest.raises(botfile.BotFileError) as probed:
        botfile.probe(tampered)
    status, _h, raw = upload(tampered)
    assert status == 400 and json.loads(raw)["error"] == str(probed.value) == "skills/greet.md does not match its declared size or hash"
    assert [n for n in os.listdir(appfetch.downloads_dir()) if not n.startswith(".")] == []

    good = _container(tmp_path / "good.fusedbot", {},
                      [("bot.json", b'{"name": "Good"}'), ("skills/greet.md", b"# Greet\nSay hi.\n")])
    status, _h, raw = upload(good)
    assert status == 200 and os.path.isfile(json.loads(raw)["path"])


# ------------------------------------------------------------------ create with a minted id ---
def test_create_refuses_a_taken_or_malformed_id(app_home):
    from fused_render.bots import bot as bm

    b = bm.create("Taken", "", "", "x", greet=False)
    with pytest.raises(ValueError, match="already a bot"):
        bm.create("Again", "", "", "x", greet=False, bot_id=b.id)
    with pytest.raises(ValueError, match="bad bot id"):
        bm.create("Bad", "", "", "x", greet=False, bot_id="../etc")
    nb = bm.create("Mine", "", "", "x", greet=False, bot_id="0123abcd")
    assert nb.id == "0123abcd" and nb.meta["name"] == "Mine"


def test_create_raising_never_adopts_a_bot_another_request_made(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, store

    src = bm.create("Src", "", "", "do things", greet=False)
    path = package.write(src.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    real, made = bm.create, {}

    def racing(*a, bot_id=None, **kw):  # another request's bot lands in the window, then ours fails unwritten
        made["other"] = real("Other", "", "", "mine", greet=False).id
        made["asked"] = bot_id
        raise RuntimeError("registry build failed")

    monkeypatch.setattr(bm, "create", racing)
    with pytest.raises(botfile.BotFileError, match="could not create the bot: registry build failed"):
        botfile.install(path, apps=[], memory=False)
    assert made["asked"] and made["asked"] != made["other"]
    assert store.list_ids() == [made["other"]]
    meta = store.read_meta(made["other"])
    assert meta["name"] == "Other" and meta["instructions"] == "mine" and "origin" not in meta


def test_create_raising_after_writing_continues_with_the_minted_bot(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, store

    src = bm.create("Src", "", "", "do things", greet=False)
    path = package.write(src.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    real, asked = bm.create, []

    def late(*a, **kw):
        asked.append(kw["bot_id"])
        real(*a, **kw)
        raise RuntimeError("first emit failed")

    monkeypatch.setattr(bm, "create", late)
    out = botfile.install(path, apps=[], memory=False)
    assert out["bot_id"] == asked[0] and "create: first emit failed" in out["warnings"]
    assert store.read_meta(out["bot_id"])["origin"]["package_sha"] == botfile._package_sha(botfile._index(path))


# ------------------------------------------------------------------ settings rows ---
def _flaky_trust_save(monkeypatch, folder: str) -> list:
    """Bot.save raises once, on the save that would record `folder` as trusted."""
    from fused_render.bots import bot as bm

    real, fired = bm.Bot.save, []

    def save(self):
        if not fired and folder in (self.meta.get("trusted_apps") or []):
            fired.append(1)
            raise OSError("disk full")
        return real(self)

    monkeypatch.setattr(bm.Bot, "save", save)
    return fired


def _package_with_app(monkeypatch, tmp_path) -> str:
    from fused_render.bots import bot as bm
    from fused_render.bots import package

    _app("app-a", "A")
    b = bm.create("Appy", "", "", "do things", greet=False)
    with b.lock:
        b.meta["apps"] = [_bundled("app-a", "A")]
        b.save()
    path = package.write(b.id, memory=False, apps=["app-a"], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    return path


def _settings_row(out: dict) -> dict:
    [row] = out["apps"]
    assert row["folder"] == "app-a" and row["ok"] is False and row["state"] == "settings"
    assert row["error"] == "installed, but the bot could not be updated: disk full"
    assert os.path.basename(row["path"]) == "app-a" and os.path.isdir(row["path"])
    assert "settings: disk full" in out["warnings"]
    return row


def _linked_once(bid: str) -> None:
    from fused_render.bots import store

    meta = store.read_meta(bid)
    assert [r["folder"] for r in meta["apps"]] == ["app-a"]
    assert meta["trusted_apps"].count("app-a") == 1


def test_new_install_whose_bot_update_fails_reports_a_settings_row_and_retries(monkeypatch, tmp_path):
    from fused_render.bots import botfile, store

    path = _package_with_app(monkeypatch, tmp_path)
    fired = _flaky_trust_save(monkeypatch, "app-a")
    out = botfile.install(path, apps=["app-a"], memory=False)
    assert fired
    _settings_row(out)
    assert store.read_meta(out["bot_id"])["origin"]["package_sha"]  # saved first, so the Retry is accepted
    again = botfile.install(path, apps=["app-a"], memory=False, bot_id=out["bot_id"])
    assert [(r["folder"], r["ok"]) for r in again["apps"]] == [("app-a", True)] and again["warnings"] == []
    _linked_once(out["bot_id"])


def test_reinstall_whose_bot_update_fails_reports_a_settings_row_and_retries(monkeypatch, tmp_path):
    from fused_render.bots import botfile, store

    path = _package_with_app(monkeypatch, tmp_path)
    bid = botfile.install(path, apps=[], memory=False)["bot_id"]
    fired = _flaky_trust_save(monkeypatch, "app-a")
    out = botfile.install(path, apps=["app-a"], memory=False, bot_id=bid)
    assert fired
    _settings_row(out)
    assert "app-a" not in (store.read_meta(bid).get("trusted_apps") or [])
    again = botfile.install(path, apps=["app-a"], memory=False, bot_id=bid)
    assert [(r["folder"], r["ok"], r["state"]) for r in again["apps"]] == [("app-a", True, "same")]
    assert again["warnings"] == []
    _linked_once(bid)


def test_new_install_whose_settings_and_trust_saves_fail_retries_after_a_reload(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, registry, store

    path = _package_with_app(monkeypatch, tmp_path)
    fired = _flaky_trust_save(monkeypatch, "app-a")
    real, settings_fired = bm.Bot.save, []

    def save(self):  # the settings save: routines set, trust not yet (the origin save carries neither)
        if not settings_fired and "routines" in self.meta and "trusted_apps" not in self.meta:
            settings_fired.append(1)
            raise OSError("disk full")
        return real(self)

    monkeypatch.setattr(bm.Bot, "save", save)
    out = botfile.install(path, apps=["app-a"], memory=False)
    assert fired and settings_fired
    _settings_row(out)
    bid = out["bot_id"]
    registry.forget(bid)  # the Retry loads the bot from disk: origin must be there, not only in memory
    assert store.read_meta(bid)["origin"]["package_sha"] == botfile._package_sha(botfile._index(path))
    again = botfile.install(path, apps=["app-a"], memory=False, bot_id=bid)
    assert [(r["folder"], r["ok"]) for r in again["apps"]] == [("app-a", True)] and again["warnings"] == []
    _linked_once(bid)


def test_origin_save_failing_removes_the_bot_and_refuses(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, store

    path = _package_with_app(monkeypatch, tmp_path)
    real = bm.Bot.save

    def save(self):
        if "origin" in self.meta:
            raise OSError("disk full")
        return real(self)

    monkeypatch.setattr(bm.Bot, "save", save)
    with pytest.raises(botfile.BotFileError, match="could not create the bot: disk full"):
        botfile.install(path, apps=[], memory=False)
    assert store.list_ids() == []


# ------------------------------------------------------------------ id races ---
def _dupe_id_folders() -> str:
    """Two folders under bots_root whose bot.json carry one id: the index resolves neither."""
    from fused_render.bots import bot as bm

    b = bm.create("Orig", "", "", "x", greet=False)
    folder = bpaths.bot_folder(b.id)
    import shutil
    shutil.copytree(folder, folder + "-copy")
    bpaths._reset_folders()
    with pytest.raises(LookupError):
        bpaths.bot_folder(b.id)
    return b.id


def test_create_refuses_an_id_two_folders_claim(app_home):
    from fused_render.bots import bot as bm

    bid = _dupe_id_folders()
    before = sorted(os.listdir(bpaths.bots_root()))
    with pytest.raises(bm.BotIdTaken, match="already a bot"):
        bm.create("Third", "", "", "x", greet=False, bot_id=bid)
    assert sorted(os.listdir(bpaths.bots_root())) == before
    for n in before:
        with open(os.path.join(bpaths.bots_root(), n, "bot.json"), encoding="utf-8") as f:
            assert json.load(f)["name"] == "Orig"


def test_mint_id_skips_an_id_two_folders_claim(app_home, monkeypatch):
    import uuid
    from fused_render.bots import botfile

    bid = _dupe_id_folders()
    fresh = "fedcba98"
    seq = iter([uuid.UUID(bid + "0" * 24), uuid.UUID(fresh + "0" * 24)])
    monkeypatch.setattr(botfile.uuid, "uuid4", lambda: next(seq))
    assert botfile._mint_id() == fresh


def test_id_taken_between_mint_and_create_is_a_retry_never_an_adoption(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, store

    src = bm.create("Src", "", "", "do things", greet=False)
    path = package.write(src.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    real = bm.create

    def sniped(*a, bot_id=None, **kw):  # another creator takes the minted id first
        real("Other", "", "", "mine", greet=False, bot_id=bot_id)
        return real(*a, bot_id=bot_id, **kw)

    monkeypatch.setattr(bm, "create", sniped)
    with pytest.raises(botfile.BotFileError, match="id taken, retry"):
        botfile.install(path, apps=[], memory=False)
    [other] = store.list_ids()
    meta = store.read_meta(other)
    assert meta["name"] == "Other" and "origin" not in meta


def test_create_raising_after_another_name_was_written_is_not_adopted(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, store

    src = bm.create("Src", "", "", "do things", greet=False)
    path = package.write(src.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    _second_machine(tmp_path, monkeypatch)
    real = bm.create

    def odd(*a, bot_id=None, **kw):  # a bot.json lands under our id, but not the one we asked for
        real("Other", "", "", "mine", greet=False, bot_id=bot_id)
        raise RuntimeError("first emit failed")

    monkeypatch.setattr(bm, "create", odd)
    with pytest.raises(botfile.BotFileError, match="could not create the bot: first emit failed"):
        botfile.install(path, apps=[], memory=False)
    [other] = store.list_ids()
    assert "origin" not in store.read_meta(other)


# ------------------------------------------------------------------ member checks ---
def _lying_container(monkeypatch, path, index, members, lie_on):
    from fused_render import appfile_container

    real = appfile_container._put_member

    def lying(data, rel, src, offset):
        entry = real(data, rel, src, offset)
        return {**entry, "sha256": "0" * 64} if lie_on(rel) else entry

    monkeypatch.setattr(appfile_container, "_put_member", lying)
    try:
        return _container(path, index, members)
    finally:
        monkeypatch.setattr(appfile_container, "_put_member", real)


def test_corrupt_memory_fails_the_full_probe_like_install(client, tmp_path, monkeypatch):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile

    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)
    path = _lying_container(monkeypatch, tmp_path / "mem.fusedbot", {},
                            [("bot.json", b'{"name": "Hand"}'), (".fused/memory.md", b"remember\n")],
                            lambda rel: rel == ".fused/memory.md")
    assert botfile.probe(path, light=True)["memory"]
    with pytest.raises(botfile.BotFileError) as probed:
        botfile.probe(path)
    with pytest.raises(botfile.BotFileError) as installed:
        botfile.install(path, apps=[], memory=True)
    assert str(probed.value) == str(installed.value) == ".fused/memory.md does not match its declared size or hash"
    with open(path, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=mem.fusedbot", f.read(), raw=True)
    assert status == 400 and json.loads(raw)["error"] == str(probed.value)


def test_corrupt_bundled_member_refuses_the_upload_but_not_the_probe(client, tmp_path, monkeypatch):
    from fused_render import appfetch
    from fused_render.bots import botfile

    # The member is not named by an `apps` row, so the full probe stages nothing: it never reads bundled members.
    path = _lying_container(monkeypatch, tmp_path / "app.fusedbot", {},
                            [("bot.json", b'{"name": "Hand"}'), ("apps/x.fused", b"x" * 5000)],
                            lambda rel: rel.startswith("apps/"))
    assert botfile.probe(path, light=True)["ok"] and botfile.probe(path)["ok"]
    with open(path, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=app.fusedbot", f.read(), raw=True)
    assert status == 400 and json.loads(raw)["error"] == "apps/x.fused does not match its declared size or hash"
    assert [n for n in os.listdir(appfetch.downloads_dir()) if not n.startswith(".")] == []

    good = _container(tmp_path / "good.fusedbot", {}, [("bot.json", b'{"name": "Hand"}'), ("apps/x.fused", b"x" * 5000)])
    with open(good, "rb") as f:
        status, _h, raw = client.post("/api/botfile/upload?name=good.fusedbot", f.read(), raw=True)
    assert status == 200


# ------------------------------------------------------------------ identity + update (docs/bots.md §17) ---
SKILL_KEEP = b"# Keep\ntrigger: keep it\n\nDo the kept thing.\n"


def _rich_bot(name="Rich"):
    """A bot with a playbook, a face, two routines and memory: everything an update replaces or keeps."""
    from fused_render.bots import bot as bm

    b = bm.create(name, "", "", "the exported instructions", greet=False)
    os.makedirs(b.skills_dir, exist_ok=True)
    with open(os.path.join(b.skills_dir, "keep.md"), "wb") as f:
        f.write(SKILL_KEEP)
    r1 = b.routine_add("check the inbox", "interval", minutes=90)
    r2 = b.routine_add("send the digest", "daily", time_s="08:30")
    with b.lock:
        b.meta["face"] = {"shape": "drop", "color": "", "icon": ""}
        b.save()
    b.set_memory("exported memory")
    return b, r1["id"], r2["id"]


def _export(bid, tmp_path, sub="out", memory=False):
    from fused_render.bots import package

    return package.write(bid, memory=memory, apps=[], file_name=None, dest_dir=str(tmp_path / sub))


def test_probe_on_the_exporting_machine_is_same_then_update(tmp_path):
    from fused_render.bots import botfile, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    blk = botfile.probe(path)["bot"]
    assert blk["state"] == "same" and blk["existing"]["id"] == b.id and blk["edited_here"] is False
    assert blk["existing"]["name"] == "Rich" and blk["existing"]["face"]["shape"] == "drop"
    assert botfile.probe(path, light=True)["bot"] == {**blk, "state": "update"}  # the peek: id match, no digest
    rb = registry.get(b.id)
    with rb.lock:
        rb.meta["instructions"] = "changed after the export"
        rb.save()
    for light in (True, False):
        blk = botfile.probe(path, light=light)["bot"]
        assert blk["state"] == "update" and blk["existing"]["id"] == b.id and blk["edited_here"] is False


def test_light_probe_is_the_id_match_alone(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, store

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    other = bm.create("Other", "", "", "x", greet=False)
    with open(store.state_path(other.id), "w", encoding="utf-8") as f:
        f.write("{not json")  # another bot's state file: the peek must never read it
    real_read = botfile._disk_meta
    read = []

    def spy(bid):
        read.append(bid)
        return real_read(bid)

    monkeypatch.setattr(botfile, "_disk_meta", spy)
    monkeypatch.setattr(package, "public_sha", lambda bid: pytest.fail("the light probe digested a bot"))
    blk = botfile.probe(path, light=True)["bot"]
    assert blk == {"state": "update", "edited_here": False,
                   "existing": {"id": b.id, "name": "Rich", "face": {"shape": "drop", "color": "", "icon": ""}}}
    assert read == [b.id]
    # A bot matched only by its origin is not looked for by the peek.
    old = _container(tmp_path / "o.fusedbot", {"origin": {"bot_id": "0badf00d", "exported_at": "x"}},
                     [("bot.json", b'{"name": "O"}')])
    assert botfile.probe(old, light=True)["bot"]["state"] == "new" and read == [b.id]
    assert os.path.exists(store.state_path(other.id))  # never moved aside


def test_full_probe_origin_scan_leaves_a_corrupt_state_file_in_place(tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, store

    b, _r1, _r2 = _rich_bot()
    path = _container(tmp_path / "o.fusedbot", {"origin": {"bot_id": "0badf00d", "exported_at": "x"}},
                      [("bot.json", b'{"name": "O"}')])
    other = bm.create("Other", "", "", "x", greet=False)
    spath = store.state_path(other.id)
    with open(spath, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert botfile.probe(path)["bot"]["state"] == "new"  # scanned every bot's origin
    with open(spath, encoding="utf-8") as f:
        assert f.read() == "{not json"
    assert not [n for n in os.listdir(os.path.dirname(spath)) if ".corrupt-" in n]


def test_update_removes_a_readme_and_icon_the_package_lacks(tmp_path):
    from fused_render import appfile
    from fused_render.bots import botfile, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)  # no README.md, no icon.svg
    folder = bpaths.bot_folder(b.id)
    with open(os.path.join(folder, "README.md"), "wb") as f:
        f.write(b"# Added here\n")
    with open(os.path.join(folder, "icon.svg"), "wb") as f:
        f.write(ICON)
    assert botfile.probe(path)["bot"]["state"] == "update"
    out = botfile.install(path, apps=[], memory=False, mode="update", target=b.id)
    assert out["ok"] and out["warnings"] == []
    assert not os.path.exists(os.path.join(folder, "README.md")) and not os.path.exists(os.path.join(folder, "icon.svg"))
    assert botfile.probe(path)["bot"] == {"state": "same", "edited_here": False,
                                          "existing": {"id": b.id, "name": "Rich",
                                                       "face": {"shape": "drop", "color": "", "icon": ""}}}
    # An icon over the cap is not copied: the bot's own goes too (with a warning), so the digests still agree.
    big = b"<svg>" + b" " * appfile.MAX_ICON_BYTES + b"</svg>"
    raw = json.dumps({"id": b.id, "name": "Rich"}).encode()
    fat = _container(tmp_path / "fat.fusedbot", {"origin": {"bot_id": b.id, "exported_at": "x"}},
                     [("bot.json", raw), ("icon.svg", big)])
    with open(os.path.join(folder, "icon.svg"), "wb") as f:
        f.write(ICON)
    out = botfile.install(fat, apps=[], memory=False, mode="update", target=b.id)
    assert any(w.startswith("icon.svg: larger than") for w in out["warnings"])
    assert any("removed to match the package" in w for w in out["warnings"])
    assert not os.path.exists(os.path.join(folder, "icon.svg"))
    assert botfile.probe(fat)["bot"]["state"] == "same"
    registry.get(b.id)  # still loads


def test_playbook_limits_refuse_the_package_everywhere(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, store

    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)
    ok = b"# P\ntrigger: p\n\nsteps\n"
    many = _container(tmp_path / "many.fusedbot", {},
                      [("bot.json", b'{"name": "Many"}')]
                      + [(f"skills/p{i}.md", ok) for i in range(bm.Bot.SKILL_MAX + 1)])
    long = _container(tmp_path / "long.fusedbot", {},
                      [("bot.json", b'{"name": "Long"}'),
                       ("skills/p.md", b"# P\ntrigger: p\n\n" + b"x" * (bm.Bot.SKILL_BODY_CAP + 1) + b"\n")])
    for path, why in ((many, "a bot holds at most"), (long, "characters of steps")):
        with pytest.raises(botfile.BotFileError, match=why):
            botfile.probe(path)
        with pytest.raises(botfile.BotFileError, match=why):
            botfile.install(path, apps=[], memory=False)
    with pytest.raises(botfile.BotFileError, match="a bot holds at most"):
        botfile.probe(many, light=True)  # the count is index-only
    assert store.list_ids() == []  # refused before anything was written
    b, _r1, _r2 = _rich_bot()
    long_up = _container(tmp_path / "long2.fusedbot", {"origin": {"bot_id": b.id, "exported_at": "x"}},
                         [("bot.json", json.dumps({"id": b.id, "name": "Rich"}).encode()),
                          ("skills/p.md", b"# P\ntrigger: p\n\n" + b"x" * (bm.Bot.SKILL_BODY_CAP + 1) + b"\n")])
    with pytest.raises(botfile.BotFileError, match="characters of steps"):
        botfile.install(long_up, apps=[], memory=False, mode="update", target=b.id)
    assert os.listdir(os.path.join(bpaths.bot_folder(b.id), "skills")) == ["keep.md"]  # untouched


def test_playbook_names_equal_but_for_case_install_once(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package

    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)
    path = _container(tmp_path / "case.fusedbot", {},
                      [("bot.json", b'{"id": "c0ffee00", "name": "Case"}'), ("skills/Greet.md", b"# A\ntrigger: a\n\nfirst\n"),
                       ("skills/greet.md", b"# B\ntrigger: b\n\nsecond\n")])
    out = botfile.install(path, apps=[], memory=False)
    assert any("differs from another only by case" in w and "greet" in w for w in out["warnings"])
    skills = os.listdir(os.path.join(bpaths.bot_folder(out["bot_id"]), "skills"))
    assert skills == ["Greet.md"]  # the first in sorted order
    assert botfile._package_public_sha(path, botfile._index(path)) == package.public_sha(out["bot_id"])
    out = botfile.install(path, apps=[], memory=False, mode="update", target=out["bot_id"])
    assert out["ok"] and os.listdir(os.path.join(bpaths.bot_folder(out["bot_id"]), "skills")) == ["Greet.md"]


def test_update_replaces_the_public_half_and_keeps_the_private(tmp_path):
    from fused_render.bots import botfile, package, registry, store

    b, r1, r2 = _rich_bot()
    path = _export(b.id, tmp_path, memory=True)
    rb = registry.get(b.id)
    # Local edits after the export: every one of them a public change the update must undo, plus private state
    # it must keep.
    with open(os.path.join(rb.skills_dir, "extra.md"), "wb") as f:
        f.write(b"# Extra\ntrigger: extra\n\nNot in the package.\n")
    with open(os.path.join(rb.skills_dir, "keep.md"), "wb") as f:
        f.write(b"# Keep\ntrigger: keep it\n\nEdited here.\n")
    rb.routine_update(r2, delete=True)
    r3 = rb.routine_add("a local one", "interval", minutes=120)["id"]
    with rb.lock:
        rb.meta["instructions"] = "edited here"
        rb.meta["face"] = {"shape": "hex", "color": "", "icon": ""}
        row = next(r for r in rb.meta["routines"] if r["id"] == r1)
        row.update(enabled=True, last=1234.0, fails=2)
        rb.meta["trusted_apps"] = ["mine"]
        rb.save()
    rb.set_memory("local memory")
    browser = rb.meta["browser_id"]
    with open(store.events_path(b.id), "rb") as f:
        events = f.read()

    out = botfile.install(path, apps=[], memory=False, mode="update", target=b.id)
    assert out["ok"] and out["updated"] is True and out["bot_id"] == b.id
    assert store.list_ids() == [b.id]  # no second bot
    rb = registry.get(b.id)
    assert rb.meta["instructions"] == "the exported instructions"
    assert rb.meta["face"]["shape"] == "drop"
    assert sorted(os.listdir(rb.skills_dir)) == ["keep.md"]  # extra.md deleted: the folder is replaced
    with open(os.path.join(rb.skills_dir, "keep.md"), "rb") as f:
        assert f.read() == SKILL_KEEP
    assert rb.memory().strip() == "local memory"  # memory unticked: kept
    with open(store.events_path(b.id), "rb") as f:
        assert f.read() == events
    assert rb.meta["trusted_apps"] == ["mine"] and rb.meta["browser_id"] == browser
    by_id = {r["id"]: r for r in rb.meta["routines"]}
    assert set(by_id) == {r1, r2} and r3 not in by_id  # the local routine is gone
    assert by_id[r1]["enabled"] is True and by_id[r1]["last"] == 1234.0 and by_id[r1]["fails"] == 2
    assert by_id[r2]["enabled"] is False and by_id[r2]["time"] == "08:30"  # back from the package, paused
    assert rb.meta["origin"]["public_sha"] == package.public_sha(b.id)
    assert botfile.probe(path)["bot"]["state"] == "same"

    out = botfile.install(path, apps=[], memory=True, mode="update", target=b.id)
    assert out["ok"] and registry.get(b.id).memory().strip() == "exported memory"  # ticked: replaced
    # The per-row Retry (re-install mode) accepts an updated bot: origin was restamped with this package.
    assert botfile.install(path, apps=[], memory=False, bot_id=b.id)["ok"]


def test_install_keeps_the_package_id_when_it_is_free(monkeypatch, tmp_path):
    from fused_render.bots import botfile, package, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    _second_machine(tmp_path, monkeypatch)
    assert botfile.probe(path)["bot"] == {"state": "new", "existing": None, "edited_here": False}
    out = botfile.install(path, apps=[], memory=False)
    assert out["bot_id"] == b.id and out["updated"] is False
    nb = registry.get(b.id)
    assert nb.meta["origin"]["bot_id"] == b.id
    assert nb.meta["origin"]["public_sha"] == package.public_sha(b.id)  # a fresh install is not "edited here"
    assert botfile.probe(path)["bot"]["state"] == "same"


def test_new_on_a_taken_id_mints_and_leaves_the_original_alone(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, registry, store

    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)  # no greeting thread outliving the test
    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    rb = registry.get(b.id)
    with rb.lock:
        rb.meta["instructions"] = "mine"
        rb.save()
    out = botfile.install(path, apps=[], memory=False, mode="new")
    assert out["bot_id"] != b.id and sorted(store.list_ids()) == sorted([b.id, out["bot_id"]])
    assert registry.get(b.id).meta["instructions"] == "mine"
    assert registry.get(out["bot_id"]).meta["instructions"] == "the exported instructions"


def test_edited_install_is_diverged_and_still_updates(monkeypatch, tmp_path):
    from fused_render.bots import botfile, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    _second_machine(tmp_path, monkeypatch)
    nb = registry.get(botfile.install(path, apps=[], memory=False)["bot_id"])
    with nb.lock:
        nb.meta["instructions"] = "edited on the second machine"
        nb.save()
    blk = botfile.probe(path)["bot"]
    assert blk["state"] == "diverged" and blk["edited_here"] is True and blk["existing"]["id"] == nb.id
    out = botfile.install(path, apps=[], memory=False, mode="update", target=nb.id)
    assert out["updated"] and registry.get(nb.id).meta["instructions"] == "the exported instructions"
    assert botfile.probe(path)["bot"] == {**blk, "state": "same", "edited_here": False}


def test_a_minted_id_bot_matches_by_origin(monkeypatch, tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    _second_machine(tmp_path, monkeypatch)
    old = bm.create("Older build", "", "", "minted", greet=False)  # an earlier build minted a fresh id
    with old.lock:
        old.meta["origin"] = {"bot_id": b.id, "exported_at": "2026-11-01T00:00:00Z", "package_sha": "x"}
        old.save()
    blk = botfile.probe(path)["bot"]
    assert blk["state"] == "update" and blk["existing"]["id"] == old.id and blk["edited_here"] is False
    out = botfile.install(path, apps=[], memory=False, mode="update", target=old.id)
    assert out["bot_id"] == old.id and registry.get(old.id).meta["instructions"] == "the exported instructions"
    assert botfile.probe(path)["bot"]["state"] == "same"


def test_a_running_bot_is_not_updated_and_super_bot_never_matches(tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, registry

    b, _r1, _r2 = _rich_bot()
    path = _export(b.id, tmp_path)
    rb = registry.get(b.id)
    rb.meta["status"] = "running"
    try:
        with pytest.raises(botfile.BotFileError, match="stop Rich before updating it"):
            botfile.install(path, apps=[], memory=False, mode="update", target=b.id)
    finally:
        rb.meta["status"] = "idle"
    with pytest.raises(botfile.BotFileError, match="did not come from this package"):
        botfile.install(path, apps=[], memory=False, mode="update", target="0000beef")

    sb = bm.create(kind="super", greet=False)
    forged = _container(tmp_path / "forged.fusedbot", {"origin": {"bot_id": sb.id, "exported_at": "x"}},
                        [("bot.json", json.dumps({"id": sb.id, "name": "Not super"}).encode())])
    assert botfile.probe(forged, light=True)["bot"]["state"] == "new"
    with pytest.raises(botfile.BotFileError, match="did not come from this package"):
        botfile.install(forged, apps=[], memory=False, mode="update", target=sb.id)


def test_public_digest_of_a_package_equals_its_bot(tmp_path):
    from fused_render.bots import botfile, package

    b, _r1, _r2 = _rich_bot()
    folder = bpaths.bot_folder(b.id)
    with open(os.path.join(folder, "README.md"), "wb") as f:
        f.write(b"# Rich\nIt does things.\n")
    with open(os.path.join(folder, "icon.svg"), "wb") as f:
        f.write(ICON)
    path = _export(b.id, tmp_path, memory=True)
    assert botfile._package_public_sha(path, botfile._index(path)) == package.public_sha(b.id)
    with open(os.path.join(folder, "README.md"), "ab") as f:
        f.write(b"more\n")
    assert botfile._package_public_sha(path, botfile._index(path)) != package.public_sha(b.id)


def test_install_route_checks_mode_and_target(client):
    status, _h, raw = client.post("/api/botfile/install", {"path": "/x.fusedbot", "apps": [], "mode": "merge"})
    assert status == 400 and b"mode must be" in raw
    status, _h, raw = client.post("/api/botfile/install", {"path": "/x.fusedbot", "apps": [], "mode": "update"})
    assert status == 400 and b"target must be" in raw
