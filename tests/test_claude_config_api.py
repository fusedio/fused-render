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
import ast
import json
import os
import subprocess
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from fused_render.claude_config import (
    claude_md,
    lib,
    memory,
    plugins,
    preferences,
    refresh_catalog,
    skills,
)
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
    monkeypatch.setattr(lib, "MARKETPLACES_DIR", str(root / "plugins" / "marketplaces"))
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


# -- memory: the project slug back to a real folder --------------------------
# Claude Code's project dirs are munged cwds (every non-alphanumeric char -> "-"),
# which is lossy: "/", ".", "_" and a literal "-" are indistinguishable
# afterwards. These pin the three ways the module answers "which folder is this?"


def _memory_project(claude_dir, slug, files=("MEMORY.md",), transcript_cwd=None):
    d = claude_dir / "projects" / slug / "memory"
    d.mkdir(parents=True)
    for name in files:
        (d / name).write_text(f"# {name}\n")
    if transcript_cwd is not None:
        (claude_dir / "projects" / slug / "abc123.jsonl").write_text(
            json.dumps({"type": "summary"}) + "\n"
            + json.dumps({"cwd": transcript_cwd, "type": "user"}) + "\n")


def test_memory_list_reads_the_real_cwd_out_of_a_transcript(client, claude_dir, tmp_path):
    # The recorded cwd is the truth and needs no guessing — note it contains a
    # "_" and a "." too, neither of which survives the munge.
    real = str(tmp_path / "work" / "my_repo.v2")
    _memory_project(claude_dir, "-tmp-work-my-repo-v2", transcript_cwd=real)

    [p] = _post(client, "memory", action="list").json()["projects"]
    assert p["project"] == "-tmp-work-my-repo-v2"  # the slug stays the identifier
    assert p["path"] == real
    assert p["pathConfirmed"] is True


def test_memory_list_reconstructs_a_hyphenated_component_against_the_disk(
        client, claude_dir, tmp_path, monkeypatch):
    # No transcript. The correct decode needs a real "-" INSIDE a component, so
    # a naive "-" -> "/" replace would answer <root>/work/fused/render.
    project = tmp_path / "work" / "fused-render"
    project.mkdir(parents=True)
    # Built with the real transform, not by hand: "_" munges to "-" too, and a
    # hand-rolled slug that forgets one is not a slug Claude would ever write.
    slug = memory._munge(str(project))
    _memory_project(claude_dir, slug)

    [p] = _post(client, "memory", action="list").json()["projects"]
    assert p["path"] == str(project)
    assert p["pathConfirmed"] is True
    # The wrong answer, explicitly: every component of this exists except the
    # last two, and it is what the one-line replace would have produced.
    assert p["path"] != str(tmp_path / "work" / "fused" / "render")


def test_memory_list_reconstructs_a_dotted_component(client, claude_dir, tmp_path):
    # "." munges to "-" like everything else, so ".openfused" splits into an
    # EMPTY segment plus "openfused" and no amount of rejoining with "-" puts
    # the dot back. The reconstruction matches munged-to-munged against the real
    # directory entries instead, which recovers it.
    project = tmp_path / ".openfused" / "workspaces" / "default"
    project.mkdir(parents=True)
    slug = memory._munge(str(project))
    assert "--" in slug  # the shape this test exists for
    _memory_project(claude_dir, slug)

    [p] = _post(client, "memory", action="list").json()["projects"]
    assert p["path"] == str(project)


def test_memory_list_shows_no_path_when_it_cannot_confirm_one(client, claude_dir):
    # No transcript, and nothing on disk matches — a memory folder can outlive
    # the project it belonged to. Inventing a path here would be a lie about
    # someone's filesystem, so there is none.
    _memory_project(claude_dir, "-nowhere-in-particular-gone")

    [p] = _post(client, "memory", action="list").json()["projects"]
    assert p["project"] == "-nowhere-in-particular-gone"
    assert p["path"] is None
    assert p["pathConfirmed"] is False


# -- plugins: the marketplace catalogs + guarded install ---------------------


def _marketplace(claude_dir, name, catalog, dotted=True):
    """Write a marketplace catalog where the real CLI clones one. `dotted`
    picks the modern <mkt>/.claude-plugin/marketplace.json over the legacy
    <mkt>/marketplace.json; `catalog` may be a str to write it malformed."""
    d = claude_dir / "plugins" / "marketplaces" / name
    if dotted:
        d = d / ".claude-plugin"
    d.mkdir(parents=True)
    body = catalog if isinstance(catalog, str) else json.dumps(catalog)
    (d / "marketplace.json").write_text(body)


def test_plugins_available_reads_every_catalog_and_joins_installed_state(client, claude_dir):
    _marketplace(claude_dir, "acme", {
        "name": "acme",
        "owner": {"name": "Acme"},
        "plugins": [
            {"name": "widget", "description": "Widgets.", "version": "1.2.3",
             "author": {"name": "Wile E."}, "category": "dev", "keywords": ["a", 7]},
            {"name": "gadget", "description": "Gadgets."},
        ],
    })
    # The legacy, undotted location must be found too.
    _marketplace(claude_dir, "legacy", {"name": "legacy", "plugins": [
        {"name": "old", "author": "A Person"},
    ]}, dotted=False)
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"widget@acme": True}}))
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"widget@acme": [{"version": "1.2.3"}]}}))

    body = _post(client, "plugins", action="available").json()
    by_id = {p["id"]: p for p in body["plugins"]}
    assert set(by_id) == {"widget@acme", "gadget@acme", "old@legacy"}
    assert body["skipped"] == []
    # The marketplace is the DIRECTORY name, which is what makes the join work.
    assert by_id["widget@acme"]["installed"] is True
    assert by_id["widget@acme"]["enabled"] is True
    assert by_id["gadget@acme"]["installed"] is False
    assert by_id["gadget@acme"]["enabled"] is False
    assert by_id["widget@acme"]["author"] == "Wile E."
    assert by_id["old@legacy"]["author"] == "A Person"  # the bare-string form
    assert by_id["widget@acme"]["keywords"] == ["a"]  # the non-string is dropped


def test_plugins_available_skips_a_broken_catalog_and_names_it(client, claude_dir):
    _marketplace(claude_dir, "good", {"plugins": [{"name": "fine"}]})
    _marketplace(claude_dir, "torn", "{ not json at all")
    _marketplace(claude_dir, "shapeless", {"plugins": "not a list"})
    (claude_dir / "plugins" / "marketplaces" / "empty").mkdir(parents=True)

    body = _post(client, "plugins", action="available").json()
    # One bad marketplace must not blank the page...
    assert [p["id"] for p in body["plugins"]] == ["fine@good"]
    # ...and must not vanish either: the page says which ones it could not read.
    assert sorted(body["skipped"]) == ["empty", "shapeless", "torn"]


def test_plugins_available_is_empty_when_no_marketplace_is_cloned(client, claude_dir):
    assert _post(client, "plugins", action="available").json() == {"plugins": [], "skipped": []}


def test_plugins_install_refuses_an_id_no_catalog_publishes(client, claude_dir, monkeypatch):
    # The CLI must never see an unvalidated id — assert it is not reached at all.
    monkeypatch.setattr(lib, "claude_cli",
                        lambda *a, **k: pytest.fail(f"claude CLI invoked with {a}"))
    _marketplace(claude_dir, "acme", {"plugins": [{"name": "widget"}]})

    assert _post(client, "plugins", action="install", id="").json() == {
        "ok": False, "error": "id required"}
    for bogus in ("widget", "widget@nope", "; rm -rf /"):
        body = _post(client, "plugins", action="install", id=bogus).json()
        assert body == {"ok": False, "error": "unknown plugin"}


def test_plugins_install_hands_a_catalog_id_to_the_cli_with_a_generous_timeout(
        client, claude_dir, monkeypatch):
    seen = {}

    def fake_cli(*args, timeout=25):
        seen["args"] = args
        seen["timeout"] = timeout
        return {"ok": True, "stdout": "installed", "stderr": ""}

    monkeypatch.setattr(lib, "claude_cli", fake_cli)
    _marketplace(claude_dir, "acme", {"plugins": [{"name": "widget"}]})

    body = _post(client, "plugins", action="install", id="widget@acme").json()
    assert body == {"ok": True, "id": "widget@acme", "stdout": "installed"}
    assert seen["args"] == ("plugin", "install", "widget@acme", "--scope", "user", "-y")
    # A marketplace clone can take a while; the 25s default would report a
    # working install as a failure.
    assert seen["timeout"] >= 60


def test_plugins_install_reports_the_cli_s_own_stderr(client, claude_dir, monkeypatch):
    monkeypatch.setattr(lib, "claude_cli",
                        lambda *a, **k: {"ok": False, "stdout": "", "stderr": "no such marketplace"})
    _marketplace(claude_dir, "acme", {"plugins": [{"name": "widget"}]})
    body = _post(client, "plugins", action="install", id="widget@acme").json()
    assert body == {"ok": False, "error": "no such marketplace"}


def test_option_shaped_names_never_reach_the_cli(client, claude_dir, monkeypatch):
    """An argv array stops COMMAND injection, not OPTION injection.

    A marketplace catalog is third-party content and `name` becomes argv, so an
    entry called "--force" would produce the id "--force@acme" — which the
    allowlist would happily confirm is "a plugin some marketplace publishes",
    and which `claude plugin install` would then read as a flag. It is dropped
    as the catalog is built, so it is not installable and not even listable.
    """
    monkeypatch.setattr(lib, "claude_cli",
                        lambda *a, **k: pytest.fail(f"claude CLI invoked with {a}"))
    _marketplace(claude_dir, "acme", {"plugins": [
        {"name": "--force"}, {"name": "-y"}, {"name": "widget"},
    ]})

    listed = _post(client, "plugins", action="available").json()["plugins"]
    assert [p["id"] for p in listed] == ["widget@acme"]

    for flag in ("--force@acme", "-y@acme"):
        assert _post(client, "plugins", action="install", id=flag).json() == {
            "ok": False, "error": "unknown plugin"}


def test_update_refuses_an_option_shaped_id_even_when_settings_lists_it(
        client, claude_dir, monkeypatch):
    # `update` checks membership in settings.json + installed_plugins.json —
    # both hand-editable, so "known" is not the same as "safe to pass as argv".
    monkeypatch.setattr(lib, "claude_cli",
                        lambda *a, **k: pytest.fail(f"claude CLI invoked with {a}"))
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"--version@acme": True}}))
    body = _post(client, "plugins", action="update", id="--version@acme").json()
    assert body == {"ok": False, "error": "unknown plugin"}


@pytest.mark.parametrize("action", ["login", "logout", "remove", "add"])
def test_mcp_refuses_an_option_shaped_server_name(client, claude_dir, monkeypatch, action):
    for fn in ("claude_cli", "claude_cli_detached"):
        monkeypatch.setattr(lib, fn,
                            lambda *a, **k: pytest.fail(f"claude CLI invoked with {a}"))
    body = _post(client, "mcp", action=action, name="--scope", json="{}").json()
    assert body == {"ok": False, "error": "invalid server name"}


def test_plugins_unknown_action_is_an_in_band_refusal(client, claude_dir):
    assert plugins.main(action="nope") == {"ok": False, "error": "unknown action: nope"}


# -- parsing `claude mcp list` -----------------------------------------------
# The CLI has no structured output, so this parser is the whole contract. It is
# pure, which makes it the cheapest thing in the package to pin down properly.

# The real line, copied from `claude mcp list` on a machine where this server is
# genuinely broken. It is the fixture that matters: the reason is joined with an
# EM DASH, so an exact-match lookup against the trailing segment classified the
# one actually-failing server as "unknown" — the UI refused to call broken the
# only thing that was.
_REAL_FAILED = (
    "plugin:github:github: https://api.githubcopilot.com/mcp/ (HTTP) - "
    "✘ Failed to connect — HTTP 400: Streamable HTTP error: Error POSTing to "
    "endpoint: bad request: Authorization header is badly formatted"
)


def test_mcp_parses_the_real_failed_line_as_failed_and_keeps_the_reason():
    [s] = lib.parse_mcp_list(_REAL_FAILED)
    assert s["status"] == "failed"
    assert s["name"] == "plugin:github:github"
    assert s["endpoint"] == "https://api.githubcopilot.com/mcp/"
    assert s["transport"] == "http"
    assert s["connected"] is False
    # The reason is the point: "failed" alone is a dead end for the user.
    assert s["statusDetail"].startswith("HTTP 400:")
    assert "Authorization header is badly formatted" in s["statusDetail"]
    # The em dash joining status to reason is not part of the reason.
    assert not s["statusDetail"].startswith("—")


@pytest.mark.parametrize("marker,phrase,status", [
    ("✔", "Connected", "connected"),
    ("!", "Needs authentication", "needs-auth"),
    ("✘", "Failed to connect", "failed"),
    ("⏸", "Pending approval", "pending"),
])
def test_mcp_status_markers_parse_bare_and_with_an_appended_reason(marker, phrase, status):
    """Every marker, both shapes. The CLI is free to append a reason to ANY
    status, so none of them may depend on the segment being exactly the phrase."""
    bare = f"srv: https://x.test/mcp (HTTP) - {marker} {phrase}"
    [s] = lib.parse_mcp_list(bare)
    assert s["status"] == status
    assert s["statusDetail"] == ""

    with_reason = f"{bare} — HTTP 500: something went wrong"
    [s] = lib.parse_mcp_list(with_reason)
    assert s["status"] == status
    assert s["statusDetail"] == "HTTP 500: something went wrong"


def test_mcp_reason_containing_a_dash_separator_keeps_the_endpoint_intact():
    # Splitting on the LAST " - " would cut inside the reason and drag the
    # endpoint along with it. The marker is the landmark, not the separator.
    line = "srv: https://x.test/mcp (HTTP) - ✘ Failed to connect — retrying - see logs"
    [s] = lib.parse_mcp_list(line)
    assert s["status"] == "failed"
    assert s["endpoint"] == "https://x.test/mcp"
    assert s["statusDetail"] == "retrying - see logs"


def test_mcp_unrecognised_marker_is_unknown_and_keeps_what_the_cli_said():
    line = "srv: https://x.test/mcp (HTTP) - ⚡ Warp speed"
    [s] = lib.parse_mcp_list(line)
    assert s["status"] == "unknown"
    # Not silently dropped: we don't know what it means, so show it verbatim.
    assert s["statusDetail"] == "⚡ Warp speed"


def test_mcp_list_skips_the_health_banner():
    parsed = lib.parse_mcp_list(
        "Checking MCP server health…\n\n"
        "srv: /usr/local/bin/thing - ✔ Connected\n"
    )
    assert [s["name"] for s in parsed] == ["srv"]
    assert parsed[0]["transport"] == "stdio"


# -- subprocess decoding: never the locale's guess ---------------------------


def _package_sources():
    pkg = os.path.dirname(os.path.abspath(lib.__file__))
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            yield name, open(os.path.join(pkg, name), encoding="utf-8").read()


def _spawn_calls(src):
    """(line no, keywords) for every subprocess.run/Popen call in a source file.

    Parsed, not grepped: the prose in this package explains the very patterns
    being banned, and a regex over raw source cannot tell a call from the
    comment describing it.
    """
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("run", "Popen")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            out.append((node.lineno, node.keywords))
    return out


def _spreads_kwargs(keywords):
    """True if the call does `**…SUBPROCESS_KWARGS`."""
    for kw in keywords:
        if kw.arg is None:  # ** unpacking
            target = kw.value
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "SUBPROCESS_KWARGS":
                return True
    return False


#: Keywords that take `subprocess` off `posix_spawn` no matter what else the
#: call passes — read straight off CPython's own condition in
#: `Popen._execute_child`. `cwd` is in here for the same reason `app_git.py`
#: bans it independently: `git -C <dir>` is the spelling that does not fork.
_FORK_FORCING = ("start_new_session", "preexec_fn", "pass_fds", "process_group", "cwd")


def _forces_fork(keywords):
    """Which fork-forcing keywords this call passes, ignoring explicit no-ops."""
    found = []
    for kw in keywords:
        if kw.arg not in _FORK_FORCING:
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value in (None, False):
            continue  # `cwd=None` says "no cwd"; it does not force anything
        found.append(kw.arg)
    return found


def _keyword_is(keywords, name, value):
    return any(
        kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is value
        for kw in keywords
    )


def test_every_subprocess_in_the_package_avoids_fork_and_pins_utf8():
    """Both halves of lib.SUBPROCESS_KWARGS, asserted at the source level —
    because neither failure reproduces in an environment a test can portably
    create, and both of them killed this feature in production:

      * close_fds=True forks, and a forking spawn in a process with libproj
        resident runs PROJ's atfork handler into a SIGSEGV before exec. The
        child dies rc=-11 with EMPTY stderr, so the MCP page said "failed to
        list MCP servers" and git_ops said "git add -A failed: " with nothing
        after the colon. pytest never has PROJ loaded, so a green suite proves
        nothing here; only the source can be checked.
      * text=True with no encoding decodes with locale.getpreferredencoding —
        ASCII in a GUI-launched process — and `claude mcp list` prints ✔.

    A call may spread SUBPROCESS_KWARGS or pass close_fds=False itself
    (archive_zip has to: it reads binary, so it cannot take the text half).

    **And close_fds=False is necessary, not sufficient.** CPython's fast path
    requires `not close_fds` AND `not start_new_session` AND no `preexec_fn`,
    `pass_fds`, `process_group` or `cwd` — so a call that spread the safe
    kwargs and then asked for a new session forked anyway, while reading as
    guarded both here and to anyone maintaining it. `claude_cli_detached` did
    exactly that, and this test passed over it: the spread was accepted as
    proof of the property it no longer had. A fork-forcing keyword now
    disqualifies a call however well-guarded the rest of it looks; the way to
    have both is `os.posix_spawn(..., setsid=True)`, which is what that
    function does now.
    """
    assert lib.SUBPROCESS_KWARGS == {
        "close_fds": False, "text": True, "encoding": "utf-8", "errors": "replace",
    }
    no_spawn_guard, forced_fork, bare_text = [], [], []
    calls = 0
    for name, src in _package_sources():
        for lineno, keywords in _spawn_calls(src):
            calls += 1
            guarded = _spreads_kwargs(keywords) or _keyword_is(keywords, "close_fds", False)
            if not guarded:
                no_spawn_guard.append(f"{name}:{lineno}")
            forcing = _forces_fork(keywords)
            if forcing:
                forced_fork.append(f"{name}:{lineno} ({', '.join(forcing)})")
            if _keyword_is(keywords, "text", True):
                bare_text.append(f"{name}:{lineno}")
    assert no_spawn_guard == []
    assert forced_fork == [], (
        "these keywords put CPython back on fork()+exec whatever close_fds says"
    )
    assert bare_text == []
    # A guard that silently matched nothing would pass forever.
    assert calls >= 6


@pytest.mark.skipif(os.name == "nt", reason="the preview runs `sh -c`")
def test_the_statusline_preview_still_runs_in_claude_dir_without_cwd(claude_dir):
    """The `cwd=` that had to go was carrying real behaviour, so it moves rather
    than disappears: a statusline command reads files beside `settings.json`
    (`cat .claude-version`, `git -C . …`) and is documented to run there.

    Written against a REAL `sh`, because the property is the shell's, not the
    call's: the directory arrives as a separate argv entry, the user's command
    is never interpolated into the script text, and the command still gets a
    fresh shell with no positional arguments of its own.
    """
    from fused_render.claude_config import statusline

    (claude_dir / "marker.txt").write_text("hello")
    (claude_dir / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command",
                       "command": 'pwd; cat marker.txt; echo "args=$#"'}}))

    out = statusline.main(action="preview")

    assert out["ok"], out
    assert os.path.realpath(lib.CLAUDE_DIR) in os.path.realpath(
        out["output"].splitlines()[0]), out["output"]
    assert "hello" in out["output"], "a relative read must resolve in CLAUDE_DIR"
    assert "args=0" in out["output"], "the command must not inherit our own argv"


@pytest.mark.skipif(os.name == "nt", reason="the preview runs `sh -c`")
def test_a_statusline_command_with_quotes_and_spaces_is_not_reparsed(claude_dir):
    """The command travels as ONE argv entry, so nothing in it can be read as
    part of the `cd` prelude — the failure a string-concatenated `cd … && …`
    would have introduced."""
    from fused_render.claude_config import statusline

    (claude_dir / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command",
                       "command": 'echo "a  b"; echo \'$1 && pwd\'; echo done'}}))

    out = statusline.main(action="preview")

    assert out["ok"], out
    assert "a  b" in out["output"], "inner quoting must survive"
    assert "$1 && pwd" in out["output"], "single-quoted text is data, not script"
    assert "done" in out["output"]


def test_git_runs_with_dash_c_rather_than_cwd():
    """app_git.py's discipline, the other half: `git -C <dir>`, never `cwd=`.
    A `cwd=` that has gone missing fails inside the spawn — before git — with
    an error that names the wrong thing."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    with mock.patch.object(subprocess, "run", fake_run):
        lib.git("status", "--porcelain")

    assert seen["argv"][:3] == ["git", "-C", lib.CLAUDE_DIR]
    assert "cwd" not in seen["kwargs"]
    assert seen["kwargs"]["close_fds"] is False


@pytest.mark.skipif(not git_available(), reason="needs git")
def test_git_log_round_trips_a_non_ascii_commit_message(claude_dir):
    lib.ensure_repo()  # the seed commit, so the edit below is a commit of its own
    (claude_dir / "settings.json").write_text(json.dumps({"model": "opus"}))
    assert lib.commit("Enable — plugin ✔")
    assert lib.log()[0]["message"] == "Enable — plugin ✔"


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
