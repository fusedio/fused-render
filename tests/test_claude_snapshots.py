"""Claude Code's file-history snapshots, shown by the `claude` template (SPEC
§34).

The capability was built into `annotate` (SPEC §17), which is superseded and
bound to nothing — its reader was written "deliberately annotate-agnostic so
`claude` and `history` can adopt the same reader" (SPEC §34), and this is that
adoption. `shared/file_history.py` is unchanged; what moves is the *offer*.

Two things are pinned here:

* **The action.** `agent.py` grows `action="snapshots"`, a thin pass-through to
  `file_history.timeline`. Deliberately NOT called `history` — that action
  already exists on this module and replays a chat SESSION TRANSCRIPT, an
  entirely different thing, and two meanings on one action name is the kind of
  collision that gets found in production.
* **The gating.** The panel is for a FILE. A folder has no checkpoint chain —
  the store keys on one absolute file path — so on a directory target the
  action refuses and the page never renders the section.
"""
import importlib.util
import os
import sys

import pytest

from _claude_history import claude_home, path_hash, write_version  # noqa: F401

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLAUDE = os.path.join(_ROOT, "fused_render", "templates", "claude")


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.syspath_prepend(
        os.path.join(_ROOT, "fused_render", "templates", "shared"))
    spec = importlib.util.spec_from_file_location(
        "_claude_agent_snapshots", os.path.join(_CLAUDE, "agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("_claude_agent_snapshots", None)


@pytest.fixture()
def source():
    with open(os.path.join(_CLAUDE, "template.html"), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------- backend


def test_a_file_with_checkpoints_returns_its_timeline(
    agent, claude_home, tmp_path
):
    target = tmp_path / "notes.md"
    target.write_text("# three\n")
    write_version(claude_home, "sess-a", str(target), "# one\n", mtime=1000)
    write_version(claude_home, "sess-a", str(target), "# two\n", mtime=2000)

    got = agent.main(action="snapshots", file=str(target))
    assert "error" not in got
    assert got["available"] is True
    assert [v["version"] for v in got["versions"]] == [2, 1]  # newest first
    assert got["hash"] == path_hash(str(target))


def test_a_file_with_no_store_is_an_ordinary_empty_state_not_an_error(
    agent, claude_home, tmp_path
):
    # The whole reason the reader returns its own empty states: "Claude Code has
    # never run here" must render as a sentence, never as a traceback overlay.
    target = tmp_path / "notes.md"
    target.write_text("hi\n")
    got = agent.main(action="snapshots", file=str(target))
    assert "error" not in got
    assert got["available"] is False
    assert got["versions"] == []
    assert got["note"]


def test_a_directory_target_is_refused(agent, claude_home, tmp_path):
    # A folder has no checkpoint chain — the store keys on one absolute FILE
    # path — so there is nothing for this action to answer with.
    folder = tmp_path / "proj"
    folder.mkdir()
    got = agent.main(action="snapshots", file=str(folder))
    assert "error" in got


def test_a_missing_target_is_refused(agent, claude_home):
    assert "error" in agent.main(action="snapshots", file="")


def test_the_snapshot_action_does_not_collide_with_the_transcript_one(agent):
    # `history` on this module replays a chat SESSION. Both must survive, under
    # names that cannot be confused for each other.
    src = open(os.path.join(_CLAUDE, "agent.py"), encoding="utf-8").read()
    assert 'action == "history"' in src
    assert 'action == "snapshots"' in src


def test_the_store_is_never_written(agent, claude_home, tmp_path):
    # Strictly read-only, like every other consumer of this store: it is Claude
    # Code's data, not ours.
    target = tmp_path / "notes.md"
    target.write_text("# now\n")
    write_version(claude_home, "sess-a", str(target), "# one\n", mtime=1000)
    root = claude_home / "file-history"
    before = sorted(
        (os.path.relpath(os.path.join(d, f), root), os.path.getsize(os.path.join(d, f)))
        for d, _, fs in os.walk(root) for f in fs
    )
    agent.main(action="snapshots", file=str(target))
    after = sorted(
        (os.path.relpath(os.path.join(d, f), root), os.path.getsize(os.path.join(d, f)))
        for d, _, fs in os.walk(root) for f in fs
    )
    assert before == after


# ---------------------------------------------------------------- the panel


def test_the_page_asks_for_snapshots_and_renders_them(source):
    assert 'action: "snapshots"' in source
    assert "renderSnapshots" in source
    assert 'id="snaps"' in source


def test_the_panel_is_only_built_for_a_file_target(source):
    # `targetNoun` is the template's own file/folder/project answer (set by
    # paneURL from the stat), so the panel hangs off that rather than a second
    # kind probe that could disagree with it. NOT `paneNoun`, which names the
    # left pane's document ("preview"/"app") and is never "file".
    body = source[source.index("async function loadSnapshots"):]
    body = body[: body.index("\n}")]
    assert 'targetNoun !== "file"' in body
    assert 'paneNoun !==' not in body
    # And it must WAIT for that answer: both boot IIFEs start together, so an
    # unordered read gets "" and the panel never shows.
    assert "await paneReady" in body


def test_the_boot_call_does_not_enrich(source):
    # Enrichment reads session transcripts (5 MB+). The boot timeline must not
    # pay that on every file open — same rule the annotate panel followed.
    call = source[source.index('action: "snapshots"'):]
    call = call[: call.index(")")]
    assert "enrich" not in call
