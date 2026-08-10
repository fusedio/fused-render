"""The Claude-config API (server/routers/claude_config.py + the
fused_render/claude_config/ package behind it): one dispatch POST per feature
module, plus the availability probe the Preferences tab gates on.

Replaces test_claude_config_mount.py. That suite covered a builtin :archive:
mount that shipped the same Python as an html+py app; the app is now a server
router, so there is no zip, no mount and no readiness poll left to test — what
took their place is the dispatch contract (allowlist, kwarg binding, error
shape) and the two port-specific decisions:

  * the settings catalog is READ from a user-writable override when one exists
    and WRITTEN only there, never into the packaged copy (site-packages is
    read-only in a real install);
  * every write is still anchored at CLAUDE_DIR, so a scratch CLAUDE_DIR fully
    isolates a test — which is what lets the git-backed actions below run for
    real rather than against a mocked subprocess.

Hermetic by construction: CLAUDE_DIR (and every module-level path derived from
it at import time) is repointed into tmp_path, HOME is repointed so
~/.claude.json discovery can be seeded, git identity comes from the
environment with the developer's ~/.gitconfig switched off, and `mdfind` is
stubbed — a real Spotlight query would return the machine's own CLAUDE.md files
and make the assertions depend on the developer's disk.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.claude_config import claude_md, lib, memory, preferences, refresh_catalog, skills
from fused_render.server import create_app
from fused_render.server.routers import claude_config as router_mod

from _git_repo import git_available

# The write guard every mutating POST in the app carries (D3): a custom header
# forces a CORS preflight, so a foreign page can't fire these blind.
HDR = {"X-Fused": "1"}


@pytest.fixture()
def claude_dir(tmp_path, monkeypatch):
    """A scratch CLAUDE_DIR, with every import-time-derived path repointed at it.

    lib.CLAUDE_DIR is resolved from the env once at import (deliberately — it is
    a constant for the process), so setting the env var in a test is too late.
    The modules that snapshot a path off it at import get the same treatment;
    missing one would silently write into the developer's real ~/.claude.
    """
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setattr(lib, "CLAUDE_DIR", str(root))
    monkeypatch.setattr(lib, "SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setattr(lib, "INSTALLED_PLUGINS_PATH",
                        str(root / "plugins" / "installed_plugins.json"))
    monkeypatch.setattr(lib, "KNOWN_MARKETPLACES_PATH",
                        str(root / "plugins" / "known_marketplaces.json"))
    monkeypatch.setattr(lib, "_LOCK_PATH", str(root / ".config-ui.lock"))
    monkeypatch.setattr(memory, "PROJECTS_DIR", str(root / "projects"))
    monkeypatch.setattr(skills, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(skills, "SKILL_LOCK_PATH",
                        str(tmp_path / ".agents" / ".skill-lock.json"))
    # HOME drives claude_md's ~/.claude.json probe (and nothing else here).
    monkeypatch.setenv("HOME", str(tmp_path))
    # Identity + no global config, so `git commit` works on a machine with an
    # empty ~/.gitconfig and a developer's own settings can't change the result
    # (same posture as tests/_git_repo.py).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Fixture Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Fixture Author")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.com")
    return root


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def no_spotlight(monkeypatch):
    monkeypatch.setattr(claude_md, "_mdfind", lambda: [])


@pytest.fixture()
def catalog_home(tmp_path, monkeypatch):
    """A per-test shell home, so the catalog override starts absent.

    conftest points FUSED_RENDER_HOME at ONE tmpdir for the whole run, so a test
    that writes the override would otherwise leak into every later test that
    asserts the packaged copy is what gets read.
    """
    home = tmp_path / "shell-home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def _post(client, module, **body):
    return client.post(f"/api/claude-config/{module}", json=body, headers=HDR)


# -- dispatch contract -------------------------------------------------------


def test_unknown_module_is_404(client):
    r = _post(client, "not_a_module", action="list")
    assert r.status_code == 404
    assert "not_a_module" in r.json()["error"]


def test_dynamic_import_is_not_reachable_through_the_url(client):
    # The allowlist is the whole point: a name that IS importable but is not a
    # feature module must be as much a 404 as gibberish.
    assert _post(client, "lib").status_code == 404
    assert _post(client, "os").status_code == 404


def test_bad_kwargs_are_400_not_500(client, claude_dir):
    r = _post(client, "git_ops", action="log", nope=1)
    assert r.status_code == 400
    assert "nope" in r.json()["error"]


def test_missing_x_fused_header_is_refused(client, claude_dir):
    r = client.post("/api/claude-config/git_ops", json={"action": "status"})
    assert r.status_code == 403


def test_empty_body_uses_each_module_s_default_action(client, claude_dir, no_spotlight):
    # No body at all must still be a valid call — main()'s defaults are the
    # documented "list/get" entry point every tab loads with.
    r = client.post("/api/claude-config/claude_md", headers=HDR)
    assert r.status_code == 200
    assert "files" in r.json()


def test_module_exception_is_500_with_the_message_only(client, claude_dir, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setitem(router_mod.MODULES, "git_ops", boom)
    r = _post(client, "git_ops", action="log")
    assert r.status_code == 500
    body = r.json()
    assert "git exploded" in body["error"]
    assert "Traceback" not in body["error"]


# -- availability probe ------------------------------------------------------


def test_status_false_when_claude_dir_is_absent(client, claude_dir):
    os.rmdir(claude_dir)
    assert client.get("/api/claude-config/status").json() == {"available": False}


def test_status_true_when_claude_dir_exists(client, claude_dir):
    assert client.get("/api/claude-config/status").json() == {"available": True}


# -- claude_md: discovery + guarded delete -----------------------------------


def test_claude_md_list_finds_the_global_file_and_project_files(
        client, claude_dir, tmp_path, no_spotlight):
    # Spotlight can never see a dotfile dir, so the global CLAUDE.md is added
    # explicitly; project dirs come from ~/.claude.json's `projects` keys.
    (claude_dir / "CLAUDE.md").write_text("# global\n")
    proj = tmp_path / "work" / "proj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("# project\n")
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {str(proj): {}}}))

    data = _post(client, "claude_md", action="list").json()
    by_path = {f["path"]: f for f in data["files"]}
    assert by_path[str(claude_dir / "CLAUDE.md")]["scope"] == "global"
    assert by_path[str(proj / "CLAUDE.md")]["scope"] == "project"
    # global sorts first — it is the one file that applies everywhere.
    assert data["files"][0]["scope"] == "global"


def test_claude_md_delete_refuses_a_non_claude_md_basename(client, claude_dir, tmp_path):
    victim = tmp_path / "secrets.env"
    victim.write_text("TOKEN=1\n")
    body = _post(client, "claude_md", action="delete", path=str(victim)).json()
    assert body["ok"] is False
    assert "not a CLAUDE.md file" in body["error"]
    assert victim.exists()


def test_claude_md_delete_removes_an_allowlisted_file(client, claude_dir, tmp_path):
    doomed = tmp_path / "elsewhere" / "CLAUDE.local.md"
    doomed.parent.mkdir()
    doomed.write_text("# scratch\n")
    body = _post(client, "claude_md", action="delete", path=str(doomed)).json()
    assert body["ok"] is True
    assert not doomed.exists()


@pytest.mark.skipif(not git_available(), reason="needs git")
def test_claude_md_commit_folds_an_edit_into_the_config_repo(client, claude_dir):
    # The page saves content through /api/fs/write, then asks this action to
    # commit the drift; the commit must land and name the file. First call
    # bootstraps the repo (the file rides the seed commit); the EDIT after
    # that is the case the action exists for.
    target = claude_dir / "CLAUDE.md"
    target.write_text("# original\n")
    _post(client, "claude_md", action="commit", path=str(target))
    target.write_text("# edited\n")
    body = _post(client, "claude_md", action="commit", path=str(target)).json()
    assert body["ok"] is True
    assert body["committed"]  # a fresh sha — the edit is in history


def test_claude_md_commit_is_a_noop_outside_the_config_repo(client, claude_dir, tmp_path):
    outside = tmp_path / "elsewhere" / "CLAUDE.md"
    outside.parent.mkdir()
    outside.write_text("# project file\n")
    body = _post(client, "claude_md", action="commit", path=str(outside)).json()
    assert body["ok"] is True
    assert body["committed"] is None


# -- preferences: schema + prefs against a seeded settings.json --------------


@pytest.mark.skipif(not git_available(), reason="needs git")
def test_preferences_get_returns_the_catalog_and_current_values(client, claude_dir):
    (claude_dir / "settings.json").write_text(json.dumps({"model": "opus"}))
    data = _post(client, "preferences", action="get").json()
    keys = [d["key"] for d in data["schema"]]
    assert "model" in keys
    assert data["prefs"]["model"] == "opus"
    # Unset catalog keys read as null rather than being absent — the page needs
    # the key to render the "using Claude's default" state.
    assert all(k in data["prefs"] for k in keys)
    # `get` also captures the baseline snapshot, so the repo exists afterwards.
    assert (claude_dir / ".git").is_dir()


@pytest.mark.skipif(not git_available(), reason="needs git")
def test_preferences_patch_writes_settings_and_rejects_unmanaged_keys(client, claude_dir):
    ok = _post(client, "preferences", action="patch",
               payload=json.dumps({"model": "haiku"})).json()
    assert ok == {"ok": True, "changed": ["model"]}
    assert json.loads((claude_dir / "settings.json").read_text())["model"] == "haiku"

    bad = _post(client, "preferences", action="patch",
                payload=json.dumps({"someRandomKey": 1})).json()
    assert bad["ok"] is False
    assert "someRandomKey" in bad["error"]


# -- the catalog override: reads fall back, writes never touch the package ----


def _fake_docs(doc_keys):
    """A settings-reference page documenting `doc_keys`, padded past the parser's
    50-key sanity floor with filler rows."""
    # Filler names must start with a letter: the row regex only accepts keys that
    # look like real settings keys.
    keys = list(doc_keys) + [f"filler{i}" for i in range(60)]
    rows = "\n".join(
        f"| `{k}` | Doc for {k}. **Default**: `false` | `example` |" for k in keys)
    return ("### Available settings\n\n| Key | Description | Example |\n"
            "|---|---|---|\n" + rows + "\n\n## Next section\n")


def test_catalog_reads_the_packaged_copy_until_an_override_exists(catalog_home):
    assert lib.catalog_read_path() == lib.packaged_catalog_path()
    override = lib.catalog_override_path()
    os.makedirs(os.path.dirname(override), exist_ok=True)
    with open(override, "w", encoding="utf-8") as f:
        json.dump([{"key": "model", "label": "Overridden"}], f)
    assert lib.catalog_read_path() == override
    assert preferences._catalog()[0]["label"] == "Overridden"


def test_refresh_writes_the_override_and_leaves_the_package_untouched(
        catalog_home, monkeypatch):
    # Only the fetch is stubbed; the parse, the >=50-key floor and the write path
    # are the real ones.
    packaged = lib.packaged_catalog_path()
    before = open(packaged, "rb").read()
    shipped = json.loads(before.decode())
    doc_keys = [d.get("docKey") or d["key"] for d in shipped]
    monkeypatch.setattr(refresh_catalog, "_fetch", lambda: _fake_docs(doc_keys))

    res = refresh_catalog.main()

    assert res["ok"] is True
    assert res["updated"] == res["total"] == len(shipped)
    assert res["undocumented"] == []
    assert res["path"] == lib.catalog_override_path()
    # The shipped copy is byte-identical: site-packages is read-only by policy.
    assert open(packaged, "rb").read() == before
    # And the refreshed catalog is what preferences now serves — same curated
    # entries (the overlay half is preserved), new doc/default half.
    assert lib.catalog_read_path() == lib.catalog_override_path()
    served = preferences._catalog()
    assert [d["key"] for d in served] == [d["key"] for d in shipped]
    assert served[0]["default"] is False
    assert served[0]["doc"].startswith("Doc for ")


def test_refresh_keeps_the_existing_catalog_when_the_docs_shape_changes(
        catalog_home, monkeypatch):
    monkeypatch.setattr(refresh_catalog, "_fetch",
                        lambda: "### Available settings\n\n| `only` | one row | x |")
    res = refresh_catalog.main()
    assert res["ok"] is False
    assert "docs shape changed" in res["error"]
    # Nothing written anywhere — a truncated refresh must not become the catalog.
    assert not os.path.exists(lib.catalog_override_path())
    assert lib.catalog_read_path() == lib.packaged_catalog_path()


def test_refresh_over_the_api_reports_a_fetch_failure_rather_than_500(
        client, catalog_home, monkeypatch):
    def offline():
        raise OSError("no network")

    monkeypatch.setattr(refresh_catalog, "_fetch", offline)
    r = _post(client, "refresh_catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "kept existing catalog" in body["error"]
