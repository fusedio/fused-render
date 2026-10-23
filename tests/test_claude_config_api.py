"""The Claude-config API (server/routers/claude_config.py + the
fused_render/claude_config/ package behind it): one dispatch POST per feature
module, plus the availability probe the Preferences tab gates on.

Covers the dispatch contract (allowlist, kwarg binding, error shape). Every
write is anchored at CLAUDE_DIR, so a scratch CLAUDE_DIR fully isolates a test.

Hermetic by construction: CLAUDE_DIR (and every module-level path derived from
it at import time) is repointed into tmp_path, HOME is repointed so
~/.claude.json discovery can be seeded, and `mdfind` is stubbed — a real
Spotlight query would return the machine's own CLAUDE.md files and make the
assertions depend on the developer's disk.
"""
import ast
import json
import os
import subprocess
import threading

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
    # HOME drives claude_md's ~/.claude.json probe (and nothing else here).
    monkeypatch.setenv("HOME", str(tmp_path))
    # The other CLIs' homes follow HOME unless their own override is set, and a
    # developer's shell may set it.
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return root


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def no_spotlight(monkeypatch):
    monkeypatch.setattr(claude_md, "_mdfind", lambda: [])


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
    r = _post(client, "preferences", action="get", nope=1)
    assert r.status_code == 400
    assert "nope" in r.json()["error"]


def test_missing_x_fused_header_is_refused(client, claude_dir):
    r = client.post("/api/claude-config/preferences", json={"action": "get"})
    assert r.status_code == 403


def test_empty_body_uses_each_module_s_default_action(client, claude_dir, no_spotlight):
    # No body at all must still be a valid call — main()'s defaults are the
    # documented "list/get" entry point every tab loads with.
    r = client.post("/api/claude-config/claude_md", headers=HDR)
    assert r.status_code == 200
    assert "files" in r.json()


def test_module_exception_is_500_with_the_message_only(client, claude_dir, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("module exploded")

    monkeypatch.setitem(router_mod.MODULES, "preferences", boom)
    r = _post(client, "preferences", action="get")
    assert r.status_code == 500
    body = r.json()
    assert "module exploded" in body["error"]
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


# -- preferences: schema + prefs against a seeded settings.json --------------


def test_preferences_get_returns_the_catalog_and_current_values(client, claude_dir):
    (claude_dir / "settings.json").write_text(json.dumps({"model": "opus"}))
    data = _post(client, "preferences", action="get").json()
    keys = [d["key"] for d in data["schema"]]
    assert "model" in keys
    assert data["prefs"]["model"] == "opus"
    # Unset catalog keys read as null rather than being absent — the page needs
    # the key to render the "using Claude's default" state.
    assert all(k in data["prefs"] for k in keys)


def test_preferences_patch_writes_settings_and_rejects_unmanaged_keys(client, claude_dir):
    ok = _post(client, "preferences", action="patch",
               payload=json.dumps({"model": "haiku"})).json()
    assert ok == {"ok": True, "changed": ["model"]}
    assert json.loads((claude_dir / "settings.json").read_text())["model"] == "haiku"

    bad = _post(client, "preferences", action="patch",
                payload=json.dumps({"someRandomKey": 1})).json()
    assert bad["ok"] is False
    assert "someRandomKey" in bad["error"]


# -- the settings-reference parser behind the catalog refresh script ---------
#
# Anthropic reshaped code.claude.com/docs/en/settings-reference.md (2026-08):
# no more table, one `### `key`` heading per setting followed by a prose
# paragraph and a bullet list. The OLD version of these tests fed a synthetic
# fixture in the table shape the parser no longer reads — which is exactly why
# the suite stayed green while `refresh_catalog` was 100% broken against the
# real page (ValueError: could not find '### Available settings' section, a
# failure). `_LIVE_DOC_SLICE` below is captured verbatim
# from the live page instead — four real sections (a plain key, a dotted
# sub-key, a backticked default, a prose "unset" default, and a
# `#### Fields for` sub-heading that must NOT parse as a key of its own).

# Captured verbatim from https://code.claude.com/docs/en/settings-reference.md
# on 2026-08-26. Do not hand-edit; if the docs reshape again, recapture.
_LIVE_DOC_SLICE = '''\
### `model`

Set the model every new session uses, so you don't have to pick one with `/model` each time. Setting it here doesn't stop you from switching mid-session. If your admin set an [organization default model](/docs/en/model-config#organization-default-model) to override user selection, you get that model even when you set this key in user, project, or local settings.

* **Scope**: [`Any file`](#scopes)
* **Type**: string, a model alias or full model ID
* **Default**: unset, so Claude Code uses your account's default model
* **Per-session overrides**: `--model` takes precedence over [`ANTHROPIC_MODEL`](/docs/en/env-vars), and both take precedence over this key for one session, including over a managed `model`; an [`availableModels`](#availablemodels) list still applies to the pick

```json settings.json theme={null}
{
  "model": "claude-sonnet-5"
}
```

A value here outranks [`ANTHROPIC_DEFAULT_MODEL`](/docs/en/model-config#set-a-default-model-for-new-sessions), which Claude Code uses only when nothing else selects a model.

### `showThinkingSummaries`

See summaries of Claude's [extended thinking](/docs/en/model-config#extended-thinking) in interactive sessions. Set it if you want the full summaries when you expand thinking with `Ctrl+O`. When unset or `false`, the Anthropic API redacts thinking blocks and Claude Code shows a collapsed stub; third-party providers don't redact.

* **Scope**: [`Any file`](#scopes)
* **Type**: Boolean
  * `true`: you see full thinking summaries when you expand thinking with `Ctrl+O`
  * `false`: the Anthropic API redacts thinking blocks and Claude Code shows a collapsed stub
* **Default**: `false`

```json settings.json theme={null}
{
  "showThinkingSummaries": true
}
```

Redaction only changes what you see, not what the model generates: to reduce thinking spend, [lower the budget or disable thinking](/docs/en/model-config#extended-thinking) instead. This setting has no effect in non-interactive mode (`-p`), the Agent SDK, or IDE extensions such as VS Code.

### `permissions.allow`

List the tool uses Claude Code approves without asking you. In an MCP rule, `*` can appear only in the tool name after the `mcp__<server>__` prefix, such as `mcp__github__get_*`; it can't appear in the server name.

* **Scope**: [`Any file`](#scopes)
* **Type**: array of permission rule strings
* **Default**: unset
* **Per-session overrides**: `--allowedTools` adds allow rules for one session, and a deny rule from any settings file still blocks a tool it names

This example approves `git diff` and lets Claude Code read your `.zshrc` without asking:

```json settings.json theme={null}
{
  "permissions": {
    "allow": ["Bash(git diff *)", "Read(~/.zshrc)"]
  }
}
```

Claude Code applies `allow` rules from a project's `.claude/settings.json` only after you accept the [workspace trust dialog](/docs/en/permissions#project-allow-rules-and-workspace-trust) for that folder.

#### Permission rule syntax

Permission rules follow the format `Tool` or `Tool(specifier)`. Claude Code evaluates `deny` rules first, then `ask`, then `allow`, and the first match decides regardless of how specific each rule is; see the [permission rule evaluation order](/docs/en/permissions#manage-permissions).

### `modelPicker`

List the models the `/model` picker offers, in the order you write them and under labels you choose, so the picker lists the models your organization runs, after the built-in lineup or instead of it. Each row's `model` is taken verbatim, so it accepts anything `--model` accepts: an alias such as `opus`, an Anthropic model ID, or a provider-format ID for Amazon Bedrock, Google Cloud's Agent Platform, Microsoft Foundry, or an LLM gateway. Requires Claude Code v2.1.242 or later.

* **Scope**: [`User or managed`](#scopes). Claude Code reads the key from managed settings, `--settings`, and user settings, and ignores it in project and local settings so a repository you clone can't relabel the picker. The highest of those three that sets the key supplies the whole lineup, and Claude Code never combines lineups from two sources.
* **Type**: object with an `options` array of rows and an optional `replaceBuiltInOptions` Boolean
* **Default**: unset, so the picker shows the built-in lineup

This example adds two Bedrock deployments after the built-in lineup, under names your team recognizes:

```json managed-settings.json theme={null}
{
  "modelPicker": {
    "options": [
      { "model": "us.anthropic.claude-opus-4-8", "label": "Opus (production)" },
      {
        "model": "us.anthropic.claude-sonnet-4-6",
        "label": "Sonnet (production)",
        "description": "Day-to-day work"
      }
    ]
  }
}
```

<span id="modelpicker-options" />

<span id="modelpicker-replacebuiltinoptions" />

#### Fields for `modelPicker`

The key takes two fields, one for the rows themselves and one for whether they replace the built-in lineup or add to it.

| Field                   | Type                                                                                  | What it does                                                                                                                                                                                                                                                                  |
| :---------------------- | :------------------------------------------------------------------------------------ | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `options`               | array of rows, each with a required `model` and an optional `label` and `description` | The rows the picker shows, in this order, except that a grayed-out row moves to the bottom. Without a `label`, Claude Code titles the row with the built-in name for a model it knows, or the model ID otherwise, and without a `description` it writes a generic second line |
| `replaceBuiltInOptions` | Boolean, default `false`                                                              | Set it to `true` to show only these rows, **Default**, and a row for the model the session is already using. Leave it unset to add these rows after the built-in lineup                                                                                                       |

With `replaceBuiltInOptions` on, Claude Code hides every other row: the built-in lineup, the rows it adds for [`availableModels`](#availablemodels) entries, the models [gateway discovery](/docs/en/llm-gateway-protocol#model-discovery) found, and [`ANTHROPIC_CUSTOM_MODEL_OPTION`](/docs/en/model-config#add-a-custom-model-option). With it off, Claude Code skips a listed model that the built-in lineup already covers. A label changes what the picker shows, not which model Claude Code runs.
'''


def test_parse_settings_reference_reads_a_real_docs_slice_verbatim():
    # Pinned against the CAPTURED page, not a synthetic stand-in — this is the
    # test that would have caught the docs reshape before it shipped broken.
    entries = refresh_catalog.parse_settings_reference(_LIVE_DOC_SLICE)

    # `#### Fields for `modelPicker`` and `#### Permission rule syntax` are
    # both 4-hash headings under real keys above — neither becomes a key of
    # its own. Exactly the four `### `key`` headings resolve.
    assert sorted(entries) == ["model", "modelPicker", "permissions.allow",
                                "showThinkingSummaries"]

    # A plain key, doc'd with a PROSE "unset" default: honest None, not the
    # literal word "unset" and not the unrelated backticked terms later in
    # the same explanatory clause.
    assert entries["model"]["default"] is None
    assert entries["model"]["doc"] == (
        "Set the model every new session uses, so you don't have to pick "
        "one with /model each time.")

    # A BACKTICKED default, coerced from JSON the way the old table's example
    # column used to be.
    assert entries["showThinkingSummaries"]["default"] is False

    # A dotted sub-key heading resolves under its own dotted name — existing
    # `docKey` lookups (e.g. `permissions.defaultMode`) depend on this.
    assert entries["permissions.allow"]["default"] is None

    # `modelPicker`'s own prose default is honestly None (unset), and its
    # "Requires Claude Code vX.Y.Z" sentence (min-version prose is the only
    # surviving signal — literal "min-version:" text is gone from the new
    # page entirely) is still picked up from its paragraph.
    assert entries["modelPicker"]["default"] is None
    assert entries["modelPicker"]["minVersion"] == "2.1.242"


# Captured verbatim from settings-reference.md alongside _LIVE_DOC_SLICE — its
# own constant because it pins a distinct failure mode: a `**Type**` bullet
# that is itself a NESTED list of every enumerated value. The old table put
# prose and the value list in one cell, so the catalog's stored `doc` used to
# be the whole enumeration — which is what the owner saw wrapping to three
# lines beside a select that already showed the same values. clean_doc must
# take the doc from the prose paragraph only, never folding the Type bullet's
# nested list in, or this bloat reappears across every enumerated key.
_LIVE_DOC_THEME_SLICE = '''\
### `theme`

Pick the color theme for the interface. Appears in `/config` as **Theme**.

* **Scope**: [`Any file`](#scopes). A value in `~/.claude.json` from an older version applies when no settings file sets it.
* **Type**: string, one of:
  * `"auto"`: matches your terminal's light or dark background
  * `"dark"`: the dark theme
  * `"light"`: the light theme
  * `"dark-daltonized"`: the dark theme with colorblind-friendly colors
  * `"light-daltonized"`: the light theme with colorblind-friendly colors
  * `"dark-ansi"`: the dark theme using only your terminal's ANSI color palette
  * `"light-ansi"`: the light theme using only your terminal's ANSI color palette
  * `"custom:<slug>"` or `"custom:<plugin-name>:<slug>"`: a custom theme from `~/.claude/themes/` or a plugin
* **Default**: `"dark"`

```json settings.json theme={null}
{
  "theme": "light-daltonized"
}
```
'''


def test_parse_settings_reference_keeps_a_nested_type_list_out_of_the_doc():
    entries = refresh_catalog.parse_settings_reference(_LIVE_DOC_THEME_SLICE)
    # The prose sentence only — none of the eight `"auto"`/`"dark"`/… bullet
    # values the Type list under it enumerates.
    assert entries["theme"]["doc"] == "Pick the color theme for the interface."
    assert entries["theme"]["default"] == "dark"


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


# -- memory: per-file frontmatter description ---------------------------------
# Round 2 of the Claude config redesign makes the memory FILE the row (grouped
# by project), not the project — so `files` carries a {name, description} pair
# per file instead of a bare name. `description` comes from the file's YAML
# frontmatter, the same shape Claude Code itself writes:
#
#   ---
#   name: some-slug
#   description: "one line about what this memory covers"
#   metadata: ...
#   ---


def _write_memory_file(claude_dir, slug, name, body, subdir="memory"):
    d = claude_dir / "projects" / slug / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_memory_list_reads_the_description_from_frontmatter(client, claude_dir):
    _write_memory_file(
        claude_dir, "-proj", "some-lesson.md",
        '---\nname: some-lesson\ndescription: "one line about the lesson"\n'
        'metadata: \n  node_type: memory\n---\n\nBody text here.\n',
    )

    [p] = _post(client, "memory", action="list").json()["projects"]
    [f] = p["files"]
    assert f["name"] == "some-lesson.md"
    assert f["description"] == "one line about the lesson"


def test_memory_list_falls_back_to_the_filename_when_frontmatter_is_missing(
        client, claude_dir):
    # A file with no --- delimiter at all — MEMORY.md, the plain index, looks
    # like this.
    _write_memory_file(claude_dir, "-proj", "MEMORY.md", "- [a lesson](a.md) — summary\n")

    [p] = _post(client, "memory", action="list").json()["projects"]
    [f] = p["files"]
    assert f["name"] == "MEMORY.md"
    assert f["description"] is None


def test_memory_list_falls_back_when_frontmatter_has_no_description_key(client, claude_dir):
    _write_memory_file(
        claude_dir, "-proj", "no-desc.md",
        "---\nname: no-desc\nmetadata: \n  node_type: memory\n---\n\nBody.\n",
    )

    [p] = _post(client, "memory", action="list").json()["projects"]
    [f] = p["files"]
    assert f["description"] is None


def test_memory_list_falls_back_when_frontmatter_never_closes(client, claude_dir):
    # A malformed file — the opening delimiter with no closing one — must not
    # be read as if the whole body were frontmatter, and must not blow up.
    _write_memory_file(
        claude_dir, "-proj", "unterminated.md",
        "---\nname: unterminated\ndescription: never actually closes\n\nrest of the file\n",
    )

    [p] = _post(client, "memory", action="list").json()["projects"]
    [f] = p["files"]
    assert f["description"] is None


def test_memory_list_never_invents_a_description_from_an_unquoted_blank_value(
        client, claude_dir):
    _write_memory_file(
        claude_dir, "-proj", "blank-desc.md",
        "---\nname: blank-desc\ndescription:\nmetadata: \n---\n\nBody.\n",
    )

    [p] = _post(client, "memory", action="list").json()["projects"]
    [f] = p["files"]
    assert f["description"] is None


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


def test_plugins_update_runs_headlessly_like_install_does(
        client, claude_dir, monkeypatch):
    """`-y` on update, not only on install. The CLI documents the flag on both,
    and re-prompts for consent when a marketplace CHANGES its declared install
    command — with stdout captured, that prompt can only be answered by the
    timeout, so the user is told the update timed out rather than that consent
    was wanted."""
    seen = {}

    def fake_cli(*args, timeout=25):
        seen["args"] = args
        return {"ok": True, "stdout": "updated", "stderr": ""}

    monkeypatch.setattr(lib, "claude_cli", fake_cli)
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"widget@acme": True}}))

    body = _post(client, "plugins", action="update", id="widget@acme").json()
    assert body == {"ok": True, "id": "widget@acme", "stdout": "updated"}
    assert seen["args"] == ("plugin", "update", "widget@acme", "--scope", "user", "-y")


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


# -- plugins: what an installed plugin puts in a session ---------------------
# `contents` is the only action that reads the plugin's own FILES rather than
# the two json files beside them, so its whole contract is on-disk layout: the
# conventional dirs, the manifest's right to relocate them, and the boundary
# that keeps a third-party manifest from pointing this at the rest of the disk.


def _installed(claude_dir, pid, root, **rec):
    """Record `pid` as installed at `root`, the way the CLI does."""
    path = claude_dir / "plugins" / "installed_plugins.json"
    doc = json.loads(path.read_text()) if path.exists() else {"plugins": {}}
    doc.setdefault("plugins", {})[pid] = [{"installPath": str(root), **rec}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def _plugin_tree(tmp_path, name, manifest=None):
    root = tmp_path / "plugin-cache" / name
    (root / ".claude-plugin").mkdir(parents=True)
    if manifest is not None:
        body = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (root / ".claude-plugin" / "plugin.json").write_text(body)
    return root


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_plugins_contents_reads_every_component_kind_off_disk(
    client, claude_dir, tmp_path
):
    root = _plugin_tree(tmp_path, "kitchen", {"name": "kitchen", "description": "All of it."})
    _write(root / "skills" / "brewing" / "SKILL.md",
           "---\nname: brew-coffee\ndescription: \"Makes coffee.\"\n---\nbody\n")
    # Nested, because a plugin is free to group its skills in subdirectories.
    _write(root / "skills" / "group" / "steeping" / "SKILL.md",
           "---\ndescription: Makes tea.\n---\n")
    _write(root / "commands" / "sub" / "pour.md", "---\ndescription: Pours.\n---\n")
    _write(root / "agents" / "taster.md", "---\nname: taster\ndescription: Tastes.\n---\n")
    _write(root / "hooks" / "hooks.json", json.dumps({"hooks": {
        "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "x"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "y"},
                                    {"type": "command", "command": "z"}]}],
    }}))
    _write(root / ".mcp.json", json.dumps({"mcpServers": {"beans": {"command": "node"}}}))
    _installed(claude_dir, "kitchen@acme", root)

    body = _post(client, "plugins", action="contents", id="kitchen@acme").json()
    assert body["ok"] is True
    assert body["description"] == "All of it."
    assert body["root"] == str(root)

    # A skill is a DIRECTORY with a SKILL.md in it; its name comes from
    # frontmatter, and falls back to the directory when the block names none.
    assert [(s["name"], s["description"]) for s in body["skills"]] == [
        ("brew-coffee", "Makes coffee."),
        ("steeping", "Makes tea."),
    ]
    assert body["skills"][0]["path"] == str(root / "skills" / "brewing" / "SKILL.md")

    # A command is INVOKED by its path, so the path is its name — a nested one
    # spells the separator the way the invocation does.
    assert body["commands"] == [{
        "name": "sub:pour", "description": "Pours.",
        "path": str(root / "commands" / "sub" / "pour.md"),
    }]
    # An agent declares its own name, so that is what it is called.
    assert body["agents"][0]["name"] == "taster"

    # One row per EVENT, counting the commands under it and naming its matchers
    # — not one row per command, which is a ${CLAUDE_PLUGIN_ROOT} string that
    # says nothing on one line.
    assert {h["name"]: h["description"] for h in body["hooks"]} == {
        "PostToolUse": "1 hook on Write",
        "SessionStart": "2 hooks",
    }
    assert body["hooks"][0]["path"] == str(root / "hooks" / "hooks.json")

    assert body["mcpServers"] == [{
        "name": "beans", "description": "node", "path": str(root / ".mcp.json"),
    }]
    # Nothing here ran the walk budget out.
    assert body["truncated"] is False


def test_plugins_contents_reads_a_bare_mcp_json(client, claude_dir, tmp_path):
    # `{"mcpServers": {...}}` is one shape a plugin's own .mcp.json comes in;
    # a BARE server map at the file's root is the other, and it is the shape
    # github@claude-plugins-official actually ships on disk. `_hooks` already
    # tolerates both shapes for hooks.json; mcpServers used to bail on the
    # wrapper key being absent, so a plugin whose entire contribution was one
    # MCP server rendered as shipping nothing at all.
    root = _plugin_tree(tmp_path, "bare-mcp", {"name": "bare-mcp"})
    _write(root / ".mcp.json", json.dumps({"github": {"type": "http", "url": "https://x"}}))
    _installed(claude_dir, "bare-mcp@acme", root)

    body = _post(client, "plugins", action="contents", id="bare-mcp@acme").json()
    assert body["mcpServers"] == [{
        "name": "github", "description": "https://x", "path": str(root / ".mcp.json"),
    }]


def test_plugins_contents_folds_a_block_scalar_description(client, claude_dir, tmp_path):
    # The frontmatter parser used to read only the key's OWN line, which is most
    # of YAML's ways of writing a string and not all of them: context-mode's
    # eight skills all write `description: |` and put the text below, so every
    # one of them displayed as a bare pipe. Folded to one line, because both
    # callers put this in a one-line slot.
    root = _plugin_tree(tmp_path, "blocky", {"name": "blocky"})
    _write(root / "skills" / "wordy" / "SKILL.md",
           "---\nname: wordy\ndescription: |\n  First line.\n  Second line.\nother: x\n---\nbody\n")
    _write(root / "skills" / "folded" / "SKILL.md",
           "---\nname: folded\ndescription: >-\n  Folded.\n---\n")
    _installed(claude_dir, "blocky@acme", root)

    body = _post(client, "plugins", action="contents", id="blocky@acme").json()
    assert [(s["name"], s["description"]) for s in body["skills"]] == [
        ("folded", "Folded."),
        ("wordy", "First line. Second line."),
    ]


def test_plugins_contents_honours_the_manifest_s_relocated_paths(
    client, claude_dir, tmp_path
):
    # Every path a manifest may move, including the ${CLAUDE_PLUGIN_ROOT} spelling
    # the CLI substitutes, and the list form.
    root = _plugin_tree(tmp_path, "moved", {
        "name": "moved",
        "skills": ["./elsewhere/skills", "${CLAUDE_PLUGIN_ROOT}/more"],
        "commands": "./cmds",
        "agents": "./bots",
        "hooks": "./cfg/hooks.json",
        "mcpServers": {"remote": {"type": "http", "url": "https://example.test/mcp"}},
    })
    _write(root / "elsewhere" / "skills" / "one" / "SKILL.md", "---\nname: one\n---\n")
    _write(root / "more" / "two" / "SKILL.md", "---\nname: two\n---\n")
    _write(root / "cmds" / "go.md", "")
    _write(root / "bots" / "bot.md", "")
    _write(root / "cfg" / "hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [{}]}]}}))
    # The conventional locations hold decoys: a manifest that names a dir
    # REPLACES the default, it does not add to it.
    _write(root / "skills" / "decoy" / "SKILL.md", "---\nname: decoy\n---\n")
    _write(root / "commands" / "decoy.md", "")
    _installed(claude_dir, "moved@acme", root)

    body = _post(client, "plugins", action="contents", id="moved@acme").json()
    assert [s["name"] for s in body["skills"]] == ["one", "two"]
    assert [c["name"] for c in body["commands"]] == ["go"]
    assert [a["name"] for a in body["agents"]] == ["bot"]
    assert [h["name"] for h in body["hooks"]] == ["Stop"]
    # Declared inline in plugin.json rather than in a .mcp.json — both are in
    # the wild — and described by its transport.
    assert body["mcpServers"] == [{
        "name": "remote", "description": "https://example.test/mcp",
        "path": str(root / ".claude-plugin" / "plugin.json"),
    }]


def test_plugins_contents_refuses_a_manifest_path_that_escapes_the_plugin(
    client, claude_dir, tmp_path
):
    # A manifest is third-party content and its path values become directories
    # this walks. One pointing out of the plugin must resolve to nothing rather
    # than to a tour of the user's disk.
    outside = tmp_path / "outside"
    _write(outside / "secret" / "SKILL.md", "---\nname: secret\n---\n")
    root = _plugin_tree(tmp_path, "nosy", {
        "name": "nosy", "skills": "../../outside", "commands": str(outside),
    })
    _installed(claude_dir, "nosy@acme", root)

    body = _post(client, "plugins", action="contents", id="nosy@acme").json()
    assert body["ok"] is True
    assert body["skills"] == []
    assert body["commands"] == []


def test_plugins_contents_refuses_a_symlinked_dir_that_escapes_the_plugin(
    client, claude_dir, tmp_path
):
    # The manifest-string escape above is lexical and `os.path.normpath`
    # catches it; a SYMLINKED component dir is a different escape entirely —
    # `skills` pointing at some real directory outside the plugin root passes
    # a purely lexical containment check (the symlink's own name sits under
    # root) even though its target does not, and `os.walk(top)` visits `top`
    # itself regardless of `followlinks` (that flag only governs descendant
    # symlinks it discovers, not the walk's own starting directory).
    outside = tmp_path / "outside-home"
    _write(outside / "secret" / "SKILL.md", "---\nname: secret\n---\n")
    root = _plugin_tree(tmp_path, "linked", {"name": "linked"})
    (root / "skills").symlink_to(outside)
    _installed(claude_dir, "linked@acme", root)

    body = _post(client, "plugins", action="contents", id="linked@acme").json()
    assert body["ok"] is True
    assert body["skills"] == []


def test_plugins_contents_reports_truncation_once_the_walk_budget_runs_out(
    client, claude_dir, tmp_path
):
    # The walk budget is a bounded-PANEL cost limit, not a per-directory one:
    # a flat commands/ dir well past the cap must come back partial AND say
    # so, rather than either hanging on a five-figure file count or silently
    # under-reporting what the plugin actually ships.
    root = _plugin_tree(tmp_path, "sprawling", {"name": "sprawling"})
    for i in range(250):
        _write(root / "commands" / f"c{i:03d}.md", "")
    _installed(claude_dir, "sprawling@acme", root)

    body = _post(client, "plugins", action="contents", id="sprawling@acme").json()
    assert body["ok"] is True
    assert body["truncated"] is True
    assert 0 < len(body["commands"]) < 250


def test_plugins_contents_survives_a_plugin_that_ships_nothing(client, claude_dir, tmp_path):
    root = _plugin_tree(tmp_path, "bare")
    _installed(claude_dir, "bare@acme", root)
    body = _post(client, "plugins", action="contents", id="bare@acme").json()
    assert body["ok"] is True
    assert (body["skills"], body["commands"], body["agents"],
            body["hooks"], body["mcpServers"]) == ([], [], [], [], [])
    assert body["truncated"] is False


def test_plugins_contents_keeps_a_broken_hooks_file_visible(client, claude_dir, tmp_path):
    # A hooks.json we cannot parse is still a hooks.json the plugin ships: the
    # row stays and opens the file that needs fixing, rather than the panel
    # quietly reporting a plugin with no hooks.
    root = _plugin_tree(tmp_path, "torn", {"name": "torn"})
    _write(root / "hooks" / "hooks.json", "{ not json")
    _installed(claude_dir, "torn@acme", root)
    body = _post(client, "plugins", action="contents", id="torn@acme").json()
    assert body["hooks"] == [{
        "name": "hooks.json", "description": "could not be read",
        "path": str(root / "hooks" / "hooks.json"),
    }]


def test_plugins_contents_survives_a_malformed_manifest(client, claude_dir, tmp_path):
    # lib.read_json lets malformed JSON raise because the user's OWN config
    # being corrupt must surface. A plugin's manifest is somebody else's file:
    # a broken one costs this panel the non-default paths, not a 500.
    root = _plugin_tree(tmp_path, "junk", "{ not json")
    _write(root / "skills" / "still" / "SKILL.md", "---\nname: still-found\n---\n")
    _installed(claude_dir, "junk@acme", root)
    body = _post(client, "plugins", action="contents", id="junk@acme").json()
    assert body["ok"] is True
    assert [s["name"] for s in body["skills"]] == ["still-found"]


def test_plugins_contents_refuses_an_id_that_is_not_installed(client, claude_dir, tmp_path):
    # The path is resolved HERE, out of installed_plugins.json, so this action
    # can never be pointed at an arbitrary directory by its caller.
    assert _post(client, "plugins", action="contents", id="").json() == {
        "ok": False, "error": "id required"}
    assert _post(client, "plugins", action="contents", id="ghost@acme").json() == {
        "ok": False, "error": "plugin is not installed"}
    # Recorded, but its files are gone — a distinct answer, because the fix is
    # different (reinstall, not "you don't have this").
    _installed(claude_dir, "gone@acme", tmp_path / "was-here")
    assert _post(client, "plugins", action="contents", id="gone@acme").json() == {
        "ok": False, "error": "plugin files are missing — reinstall it"}


def test_plugins_contents_skips_the_dirs_that_never_hold_components(
    client, claude_dir, tmp_path
):
    # A plugin root is third-party content of unbounded size and this runs on a
    # UI request: node_modules is the one that actually shows up (context-mode
    # ships one), and walking it would cost a panel a five-figure file count.
    root = _plugin_tree(tmp_path, "heavy", {"name": "heavy"})
    _write(root / "skills" / "real" / "SKILL.md", "---\nname: real\n---\n")
    _write(root / "skills" / "node_modules" / "dep" / "SKILL.md", "---\nname: dep\n---\n")
    _write(root / "skills" / ".hidden" / "SKILL.md", "---\nname: hidden\n---\n")
    _installed(claude_dir, "heavy@acme", root)
    body = _post(client, "plugins", action="contents", id="heavy@acme").json()
    assert [s["name"] for s in body["skills"]] == ["real"]


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
        list MCP servers". pytest never has PROJ loaded, so a green suite proves
        nothing here; only the source can be checked.
      * text=True with no encoding decodes with locale.getpreferredencoding —
        ASCII in a GUI-launched process — and `claude mcp list` prints ✔.

    A call may spread SUBPROCESS_KWARGS or pass close_fds=False itself.
    """
    assert lib.SUBPROCESS_KWARGS == {
        "close_fds": False, "text": True, "encoding": "utf-8", "errors": "replace",
    }
    no_spawn_guard, bare_text = [], []
    calls = 0
    for name, src in _package_sources():
        for lineno, keywords in _spawn_calls(src):
            calls += 1
            guarded = _spreads_kwargs(keywords) or _keyword_is(keywords, "close_fds", False)
            if not guarded:
                no_spawn_guard.append(f"{name}:{lineno}")
            if _keyword_is(keywords, "text", True):
                bare_text.append(f"{name}:{lineno}")
    assert no_spawn_guard == []
    assert bare_text == []
    # A guard that silently matched nothing would pass forever.
    assert calls >= 3


# -- mcp list: cached instantly, refreshed in the background --------------------
# `claude mcp list` health-checks every server (~11 s), so the tab gets the last
# answer at once (`stale`) and asks again with `fresh`.

_MCP_LINE = "srv: https://x.test/mcp (HTTP) - ✓ Connected\n"


@pytest.fixture()
def mcp_cache(tmp_path, monkeypatch):
    from fused_render.claude_config import mcp as mcp_mod

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "mcp-home"))
    mcp_mod._wait_idle()
    yield mcp_mod
    mcp_mod._wait_idle()


def _count_cli(monkeypatch, stdout=_MCP_LINE, ok=True, delay=0.0):
    import threading as _t
    import time as _time

    calls = []
    lock = _t.Lock()

    def fake(*args, timeout=25):
        with lock:
            calls.append(args)
        _time.sleep(delay)
        return {"ok": ok, "stdout": stdout, "stderr": "" if ok else "boom"}

    monkeypatch.setattr(lib, "claude_cli", fake)
    return calls


def test_mcp_list_without_a_cache_blocks_and_writes_it(client, mcp_cache, monkeypatch):
    calls = _count_cli(monkeypatch)
    body = _post(client, "mcp", action="list").json()
    assert body["ok"] and body["stale"] is False and body["servers"][0]["name"] == "srv"
    assert len(calls) == 1
    assert mcp_cache._read_cache()["servers"][0]["name"] == "srv"


def test_mcp_list_with_a_cache_returns_stale_at_once_and_refreshes_once(
        client, mcp_cache, monkeypatch):
    _count_cli(monkeypatch)
    _post(client, "mcp", action="list")
    calls = _count_cli(monkeypatch, stdout="other: https://y.test/mcp (HTTP) - ✓ Connected\n")
    body = _post(client, "mcp", action="list").json()
    assert body["ok"] and body["stale"] is True
    assert body["servers"][0]["name"] == "srv" and isinstance(body["checkedAt"], float)
    mcp_cache._wait_idle()
    assert len(calls) == 1
    assert mcp_cache._read_cache()["servers"][0]["name"] == "other"


def test_mcp_list_fresh_single_flights_concurrent_calls(client, mcp_cache, monkeypatch):
    import threading

    calls = _count_cli(monkeypatch, delay=0.3)
    results = []

    def go():
        results.append(mcp_cache.main(action="list", fresh=True))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert [r["stale"] for r in results] == [False, False]
    assert mcp_cache.main(action="list", fresh="true")["stale"] is False


@pytest.mark.parametrize("action,extra", [
    ("add", {"json": "{}"}), ("remove", {}), ("logout", {})])
def test_mcp_changes_drop_the_cache_on_success_only(
        client, mcp_cache, monkeypatch, action, extra):
    _count_cli(monkeypatch)
    _post(client, "mcp", action="list")
    assert mcp_cache._read_cache() is not None
    _count_cli(monkeypatch, ok=False)
    _post(client, "mcp", action=action, name="srv", **extra)
    assert mcp_cache._read_cache() is not None, "a failed change keeps the cache"
    _count_cli(monkeypatch, ok=True)
    _post(client, "mcp", action=action, name="srv", **extra)
    assert mcp_cache._read_cache() is None


def test_mcp_failed_refresh_leaves_the_cache_alone(client, mcp_cache, monkeypatch):
    _count_cli(monkeypatch)
    _post(client, "mcp", action="list")
    _count_cli(monkeypatch, ok=False)
    body = _post(client, "mcp", action="list", fresh=True).json()
    assert body == {"ok": False, "error": "boom"}
    assert mcp_cache._read_cache()["servers"][0]["name"] == "srv"


# -- skills: the user's own plus every enabled plugin's -----------------------


def _enable(claude_dir, **flags):
    (claude_dir / "settings.json").write_text(json.dumps({"enabledPlugins": flags}))


def _plugin_with_skills(claude_dir, tmp_path, pid, slugs, **extra):
    root = _plugin_tree(tmp_path, pid.split("@")[0])
    for slug in slugs:
        _write(root / "skills" / slug / "SKILL.md",
               f"---\nname: {slug}\ndescription: {slug} from {pid}.\n---\nbody\n")
    _installed(claude_dir, pid, root, **extra)
    return root


def test_skills_list_merges_user_and_enabled_plugin_skills(client, claude_dir, tmp_path):
    _write(claude_dir / "skills" / "mine" / "SKILL.md", "---\nname: mine\ndescription: Own.\n---\n")
    root = _plugin_with_skills(claude_dir, tmp_path, "utilities@local", ["unslop", "caveman"])
    _plugin_with_skills(claude_dir, tmp_path, "off@local", ["hidden"])
    # An old cache dir next to the current install is never listed.
    _write(tmp_path / "plugin-cache" / "utilities-old" / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    _enable(claude_dir, **{"utilities@local": True, "off@local": False})

    got = _post(client, "skills", action="list").json()["skills"]
    assert [(s["source"], s.get("plugin"), s["slug"]) for s in got] == [
        ("user", None, "mine"), ("plugin", "utilities", "caveman"), ("plugin", "utilities", "unslop"),
    ]
    plug = got[2]
    assert plug["marketplace"] == "local"
    assert plug["description"] == "unslop from utilities@local."
    assert plug["path"] == str(root / "skills" / "unslop")
    assert plug["linked"] is False


def test_skills_read_and_files_work_for_plugin_skills(client, claude_dir, tmp_path):
    root = _plugin_with_skills(claude_dir, tmp_path, "utilities@local", ["unslop"])
    _write(root / "skills" / "unslop" / "extra.md", "more")
    _enable(claude_dir, **{"utilities@local": True})
    r = _post(client, "skills", action="read", slug="unslop", source="plugin",
              plugin="utilities", marketplace="local").json()
    assert r["ok"] is True and r["text"].startswith("---")
    assert r["files"] == ["SKILL.md", "extra.md"]
    r = _post(client, "skills", action="read", slug="unslop", source="plugin",
              plugin="utilities", path="../../.claude-plugin/plugin.json").json()
    assert r["ok"] is False
    r = _post(client, "skills", action="read", slug="nope", source="plugin", plugin="utilities").json()
    assert r["ok"] is False


def test_skills_write_and_delete_refuse_plugin_skills(client, claude_dir, tmp_path):
    root = _plugin_with_skills(claude_dir, tmp_path, "utilities@local", ["unslop"])
    _enable(claude_dir, **{"utilities@local": True})
    for action in ("write", "delete"):
        r = _post(client, "skills", action=action, slug="unslop", source="plugin",
                  plugin="utilities", path="SKILL.md", text="x").json()
        assert r["ok"] is False and "read-only" in r["error"]
    assert (root / "skills" / "unslop" / "SKILL.md").read_text().endswith("body\n")


def test_skills_slug_shared_by_user_and_plugin_is_addressed_by_source(client, claude_dir, tmp_path):
    _write(claude_dir / "skills" / "unslop" / "SKILL.md", "---\nname: unslop\n---\nuser copy\n")
    _plugin_with_skills(claude_dir, tmp_path, "utilities@local", ["unslop"])
    _plugin_with_skills(claude_dir, tmp_path, "other@local", ["unslop"])
    _enable(claude_dir, **{"utilities@local": True, "other@local": True})

    assert len(_post(client, "skills", action="list").json()["skills"]) == 3
    user = _post(client, "skills", action="read", slug="unslop").json()
    assert user["text"].endswith("user copy\n")
    for plugin in ("utilities", "other"):
        r = _post(client, "skills", action="read", slug="unslop", source="plugin", plugin=plugin).json()
        assert f"{plugin}@local" in r["text"]
    # Writing the user copy leaves the plugin copies alone.
    assert _post(client, "skills", action="write", slug="unslop", path="SKILL.md", text="new").json()["ok"]
    assert _post(client, "skills", action="read", slug="unslop").json()["text"] == "new"


# -- skills across the CLIs: readers, dedupe, placement --------------------------


def _skill(path, name=None, desc="d"):
    _write(path / "SKILL.md", f"---\nname: {name or path.name}\ndescription: {desc}\n---\nbody\n")
    return path


def _rows(client):
    return {s["slug"]: s for s in _post(client, "skills", action="list").json()["skills"]}


def test_skills_list_reads_every_cli_dir_and_names_who_reads_each(client, claude_dir, tmp_path):
    _skill(claude_dir / "skills" / "solo")
    _skill(tmp_path / ".agents" / "skills" / "shared")
    _skill(tmp_path / ".copilot" / "skills" / "cop")
    _skill(tmp_path / ".codex" / "skills" / "cod")
    # Dot-dirs (Codex's bundled `.system`) are not the user's skills.
    _skill(tmp_path / ".codex" / "skills" / ".system" / "bundled")
    _skill(tmp_path / ".agents" / "skills" / ".hidden")
    rows = _rows(client)
    assert set(rows) == {"solo", "shared", "cop", "cod"}
    assert rows["solo"]["readers"] == ["claude"]
    assert rows["shared"]["readers"] == ["copilot", "codex", "gemini"]
    assert rows["cop"]["readers"] == ["copilot"]
    assert rows["cod"]["readers"] == ["codex"]
    assert rows["cod"]["location"] == str(tmp_path / ".codex" / "skills")
    assert all(r["source"] == "user" for r in rows.values())


def test_skills_list_makes_a_symlink_across_dirs_one_row_with_the_union_of_readers(
    client, claude_dir, tmp_path
):
    real = _skill(tmp_path / ".agents" / "skills" / "shared")
    (claude_dir / "skills").mkdir()
    (claude_dir / "skills" / "shared").symlink_to(real)
    (tmp_path / ".codex" / "skills").mkdir(parents=True)
    (tmp_path / ".codex" / "skills" / "shared").symlink_to(real)
    got = _post(client, "skills", action="list").json()["skills"]
    assert len(got) == 1
    row = got[0]
    assert row["readers"] == ["claude", "copilot", "codex", "gemini"]
    # The row stands on the real copy, so editing it edits the one file.
    assert row["path"] == str(real) and row["location"] == str(real.parent)
    assert row["linked"] is True
    assert sorted(row["paths"]) == sorted([
        str(real), str(claude_dir / "skills" / "shared"),
        str(tmp_path / ".codex" / "skills" / "shared")])


def test_skills_list_honours_copilot_home_and_codex_home(client, claude_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cop-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cod-home"))
    _skill(tmp_path / "cop-home" / "skills" / "c1")
    _skill(tmp_path / "cod-home" / "skills" / "c2")
    _skill(tmp_path / ".copilot" / "skills" / "ignored")
    rows = _rows(client)
    assert set(rows) == {"c1", "c2"}
    assert rows["c1"]["readers"] == ["copilot"] and rows["c2"]["readers"] == ["codex"]


def test_skills_list_marks_plugin_skills_as_read_by_claude(client, claude_dir, tmp_path):
    _plugin_with_skills(claude_dir, tmp_path, "utilities@local", ["unslop"])
    _enable(claude_dir, **{"utilities@local": True})
    assert _rows(client)["unslop"]["readers"] == ["claude"]


def test_skills_read_write_address_a_user_skill_in_any_cli_dir(client, claude_dir, tmp_path):
    _skill(tmp_path / ".agents" / "skills" / "shared")
    r = _post(client, "skills", action="read", slug="shared").json()
    assert r["ok"] and "body" in r["text"]
    assert _post(client, "skills", action="write", slug="shared", path="SKILL.md", text="new").json()["ok"]
    assert (tmp_path / ".agents" / "skills" / "shared" / "SKILL.md").read_text() == "new"


def test_skills_delete_removes_the_copy_and_every_link_to_it(client, claude_dir, tmp_path):
    real = _skill(tmp_path / ".agents" / "skills" / "shared")
    (claude_dir / "skills").mkdir()
    (claude_dir / "skills" / "shared").symlink_to(real)
    keep = _skill(claude_dir / "skills" / "keep")
    row = _rows(client)["shared"]
    r = _post(client, "skills", action="delete", slug="shared", location=row["location"]).json()
    assert r["ok"] is True
    assert not real.exists()
    assert not os.path.lexists(claude_dir / "skills" / "shared")
    assert keep.exists()


def test_skills_delete_of_a_link_to_an_outside_dir_leaves_the_target(client, claude_dir, tmp_path):
    outside = _skill(tmp_path / "elsewhere" / "mine")
    (claude_dir / "skills").mkdir()
    (claude_dir / "skills" / "mine").symlink_to(outside)
    assert _post(client, "skills", action="delete", slug="mine").json()["ok"]
    assert not os.path.lexists(claude_dir / "skills" / "mine")
    assert outside.exists()


def _prefs_on(monkeypatch, *clis):
    monkeypatch.setattr(
        skills, "_enabled_clis", lambda: [c for c in ("claude", "copilot", "codex") if c in clis])


def test_skills_create_puts_one_copy_where_the_enabled_clis_read_it(client, claude_dir, tmp_path, monkeypatch):
    _prefs_on(monkeypatch, "claude")
    assert _post(client, "skills", action="create", slug="solo").json()["ok"]
    assert (claude_dir / "skills" / "solo" / "SKILL.md").is_file()
    assert not (tmp_path / ".agents").exists()

    _prefs_on(monkeypatch, "claude", "codex")
    assert _post(client, "skills", action="create", slug="both").json()["ok"]
    real = tmp_path / ".agents" / "skills" / "both"
    assert (real / "SKILL.md").is_file()
    assert (claude_dir / "skills" / "both").is_symlink()
    assert os.path.realpath(claude_dir / "skills" / "both") == str(real)

    _prefs_on(monkeypatch, "copilot")
    assert _post(client, "skills", action="create", slug="cop").json()["ok"]
    assert (tmp_path / ".agents" / "skills" / "cop" / "SKILL.md").is_file()
    assert not os.path.lexists(claude_dir / "skills" / "cop")
    # A taken slug is refused, in any of the target dirs.
    assert _post(client, "skills", action="create", slug="solo").json()["ok"] is False


def _cloned_marketplace(claude_dir, name, entries, registered=True):
    _marketplace(claude_dir, name, {"name": name, "plugins": entries})
    if registered:
        known = claude_dir / "plugins" / "known_marketplaces.json"
        have = json.loads(known.read_text()) if known.exists() else {}
        have[name] = {"source": {"source": "github", "repo": f"x/{name}"}}
        known.write_text(json.dumps(have))
    return claude_dir / "plugins" / "marketplaces" / name


def test_skills_available_walks_cloned_marketplaces_and_skips_remote_sources(client, claude_dir):
    mkt = _cloned_marketplace(claude_dir, "acme", [
        {"name": "kit", "source": "./plugins/kit"},
        {"name": "remote", "source": {"source": "github", "repo": "a/b"}},
        {"name": "escape", "source": "../../outside"},
    ])
    _skill(mkt / "plugins" / "kit" / "skills" / "brew", name="Brew", desc="Makes coffee.")
    _skill(mkt / "plugins" / "kit" / "skills" / "own")
    _skill(claude_dir / "skills" / "own")
    body = _post(client, "skills", action="available").json()
    by_slug = {s["slug"]: s for s in body["skills"]}
    assert set(by_slug) == {"brew", "own"}
    brew = by_slug["brew"]
    assert (brew["name"], brew["description"], brew["plugin"], brew["marketplace"]) == (
        "Brew", "Makes coffee.", "kit", "acme")
    assert (brew["pluginInstalled"], brew["added"]) == (False, False)
    # Already in a user skills dir.
    assert by_slug["own"]["added"] is True
    assert body["skipped"] == 2


def test_skills_available_ignores_a_directory_no_marketplace_names(client, claude_dir):
    mkt = _cloned_marketplace(claude_dir, "acme", [{"name": "kit", "source": "./kit"}])
    _skill(mkt / "kit" / "skills" / "brew")
    stray = _cloned_marketplace(
        claude_dir, "surya-local..clone", [{"name": "ghost", "source": "./ghost"}], registered=False)
    _skill(stray / "ghost" / "skills" / "boo")
    skills_found = _post(client, "skills", action="available").json()["skills"]
    assert [(s["marketplace"], s["slug"]) for s in skills_found] == [("acme", "brew")]


def test_skills_available_carries_the_plugin_description(client, claude_dir):
    mkt = _cloned_marketplace(
        claude_dir, "acme", [{"name": "kit", "source": "./kit", "description": "A kit."}])
    _skill(mkt / "kit" / "skills" / "brew")
    assert _post(client, "skills", action="available").json()["skills"][0]["pluginDescription"] == "A kit."


def test_skills_available_counts_a_skill_of_an_enabled_plugin_as_installed(client, claude_dir, tmp_path):
    mkt = _cloned_marketplace(claude_dir, "acme", [{"name": "kit", "source": "./kit"}])
    _skill(mkt / "kit" / "skills" / "brew")
    _installed(claude_dir, "kit@acme", mkt / "kit")
    _enable(claude_dir, **{"kit@acme": True})
    brew = _post(client, "skills", action="available").json()["skills"][0]
    assert (brew["pluginInstalled"], brew["added"]) == (True, False)


def _brew_marketplace(claude_dir):
    mkt = _cloned_marketplace(claude_dir, "acme", [{"name": "kit", "source": "./kit"}])
    brew = _skill(mkt / "kit" / "skills" / "brew")
    _write(brew / "refs" / "a.md", "ref")
    return mkt, brew


def _add(client, slug="brew"):
    return _post(client, "skills", action="add", marketplace="acme", plugin="kit", slug=slug).json()


def test_skills_add_copies_to_claude_when_only_claude_is_on(client, claude_dir, tmp_path, monkeypatch):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "claude")
    assert _add(client)["ok"] is True
    dest = claude_dir / "skills" / "brew"
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").is_file() and (dest / "refs" / "a.md").read_text() == "ref"
    assert not (tmp_path / ".agents").exists()


def test_skills_add_keeps_one_copy_in_agents_and_links_claude_to_it(client, claude_dir, tmp_path, monkeypatch):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "claude", "codex")
    assert _add(client)["ok"] is True
    real = tmp_path / ".agents" / "skills" / "brew"
    assert (real / "SKILL.md").is_file() and not real.is_symlink()
    link = claude_dir / "skills" / "brew"
    assert link.is_symlink() and os.path.realpath(link) == str(real)
    # And it lists as the one row it is.
    assert _rows(client)["brew"]["readers"] == ["claude", "copilot", "codex", "gemini"]


def test_skills_add_without_claude_makes_no_claude_link(client, claude_dir, tmp_path, monkeypatch):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "copilot")
    assert _add(client)["ok"] is True
    assert (tmp_path / ".agents" / "skills" / "brew" / "SKILL.md").is_file()
    assert not os.path.lexists(claude_dir / "skills" / "brew")


def test_skills_add_refuses_a_slug_that_already_exists_in_a_target(client, claude_dir, tmp_path, monkeypatch):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "claude", "codex")
    _skill(claude_dir / "skills" / "brew")
    r = _add(client)
    assert r["ok"] is False and "already" in r["error"]
    assert not (tmp_path / ".agents" / "skills" / "brew").exists()
    # A dangling link is as taken as a skill.
    (claude_dir / "skills" / "brew").rename(claude_dir / "skills" / "moved")
    (claude_dir / "skills" / "brew").symlink_to(tmp_path / "nowhere")
    assert _add(client)["ok"] is False


def test_skills_add_refuses_what_no_cloned_marketplace_publishes(client, claude_dir, monkeypatch):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "claude")
    assert _add(client, slug="nope")["ok"] is False
    assert _post(client, "skills", action="add", marketplace="acme", plugin="nope", slug="brew").json()["ok"] is False
    assert _post(client, "skills", action="add", marketplace="../x", plugin="kit", slug="brew").json()["ok"] is False
    assert _add(client, slug="../brew")["ok"] is False
    assert not (claude_dir / "skills").exists()


def test_skills_add_does_not_follow_a_symlink_out_of_the_clone(client, claude_dir, tmp_path, monkeypatch):
    _, brew = _brew_marketplace(claude_dir)
    secret = tmp_path / "secret"
    _write(secret / "key.txt", "hunter2")
    (brew / "leak").symlink_to(secret)
    (brew / "leak.txt").symlink_to(secret / "key.txt")
    _prefs_on(monkeypatch, "claude")
    assert _add(client)["ok"] is True
    dest = claude_dir / "skills" / "brew"
    assert not os.path.lexists(dest / "leak") and not os.path.lexists(dest / "leak.txt")
    assert (dest / "SKILL.md").is_file()


def test_skills_add_refuses_a_skill_over_the_size_budget_and_leaves_nothing(
    client, claude_dir, monkeypatch
):
    _brew_marketplace(claude_dir)
    _prefs_on(monkeypatch, "claude")
    monkeypatch.setattr(skills, "_COPY_MAX_FILES", 1)
    r = _add(client)
    assert r["ok"] is False and "large" in r["error"]
    assert not (claude_dir / "skills").exists() or not list((claude_dir / "skills").iterdir())


def test_plugins_list_carries_the_description_from_the_cloned_catalog(client, claude_dir):
    _cloned_marketplace(claude_dir, "acme", [
        {"name": "widget", "description": "Widgets.", "source": "./w"},
        {"name": "gadget", "source": "./g"},
    ])
    _installed(claude_dir, "widget@acme", claude_dir / "x")
    _installed(claude_dir, "gadget@acme", claude_dir / "x")
    _installed(claude_dir, "orphan@gone", claude_dir / "x")
    by_id = {p["id"]: p for p in _post(client, "plugins", action="list").json()["plugins"]}
    assert by_id["widget@acme"]["description"] == "Widgets."
    assert by_id["gadget@acme"]["description"] == ""
    assert by_id["orphan@gone"]["description"] == ""


def test_rejoin_folds_case_only_for_drive_rooted_slugs(tmp_path):
    (tmp_path / "Work" / "Repo").mkdir(parents=True)
    # A drive-rooted slug (case-insensitive volume): the slug may spell it any way.
    assert memory._rejoin(["work", "repo"], str(tmp_path), fold=True) == str(tmp_path / "Work" / "Repo")
    # POSIX stays exact: "work" is not "Work".
    assert memory._rejoin(["work", "repo"], str(tmp_path)) is None
