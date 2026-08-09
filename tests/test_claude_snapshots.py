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

And the panel is INTERACTIVE: a row can be gone back to. That half is
`snapshot_plan` + `snapshot_revert`, the same two-call contract annotate pinned
(SPEC §34, D194) — the plan chooses and describes, the write only applies an id
the plan already handed out — plus the pre-restore stash into the sidecar this
module already read-merge-writes.
"""
import importlib.util
import json
import os
import sys

import pytest

from _claude_history import claude_home, path_hash, write_version  # noqa: F401

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLAUDE = os.path.join(_ROOT, "fused_render", "templates", "claude")

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # The sidecar (and its revertStash) live under home_dir()/sidecar/ (D205),
    # so pin FUSED_RENDER_HOME or a revert test writes into the real
    # ~/.fused-render. The mounts dir hangs off the same root, which is also how
    # the mount-backed refusal below gets a path to point at.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


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


# --------------------------------------------------- going back to a snapshot


def _target(tmp_path, content, name="notes.md"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


def f_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_a_plan_describes_the_write_for_the_row_that_was_clicked(
    agent, claude_home, tmp_path
):
    f = _target(tmp_path, "unsaved\nwork\n")
    write_version(claude_home, "sess-a", f, "wanted\n")
    plan = agent.main(action="snapshot_plan", file=f, version_id="sess-a@v1")
    assert plan["ok"] is True
    assert plan["id"] == "sess-a@v1"
    assert plan["action"] == "restore"
    # The confirm step must be able to say WHAT changes, not only how much.
    assert any(ln.startswith("+wanted") for ln in plan["diff"]["lines"])
    # ...and whether a copy of the current bytes will be kept, BEFORE the click.
    assert plan["stash"] is True
    # The sharp one: these on-disk bytes are in no checkpoint at all.
    assert plan["unique_current"] is True


def test_a_plan_without_a_version_id_is_refused(agent, claude_home, tmp_path):
    # This panel is a list of rows; every plan comes from one. Nothing here
    # picks a target on the user's behalf.
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "sess-a", f, "old\n")
    assert "error" in agent.main(action="snapshot_plan", file=f)


def test_a_revert_without_a_plan_provided_id_is_refused(
    agent, claude_home, tmp_path
):
    # The whole contract: the write never chooses. A destructive action with no
    # plan echo is one where the user confirmed nothing in particular.
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "sess-a", f, "old\n")
    out = agent.main(action="snapshot_revert", file=f)
    assert "error" in out
    assert f_text(f) == "disk\n"


def test_a_plan_then_a_revert_puts_that_version_back_on_disk(
    agent, claude_home, tmp_path
):
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "sess-a", f, "older\n", mtime=1000)
    write_version(claude_home, "sess-a", f, "disk\n", mtime=2000)

    plan = agent.main(action="snapshot_plan", file=f, version_id="sess-a@v1")
    out = agent.main(action="snapshot_revert", file=f, version_id=plan["id"])
    assert out["ok"] is True and out["id"] == "sess-a@v1"
    assert f_text(f) == "older\n"
    # The POST-write timeline rides along, so the panel never spends a round
    # trip showing the pre-revert position back to the user who just clicked.
    assert out["timeline"]["available"] is True


def test_the_previous_content_is_stashed_before_the_write(
    agent, claude_home, tmp_path
):
    # Current bytes are frequently in NO checkpoint, so the restore can vaporize
    # work that exists nowhere else — the stash is the second line of defence
    # behind the confirm step.
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "sess-a", f, "wanted\n")
    out = agent.main(action="snapshot_revert", file=f, version_id="sess-a@v1",
                     confirm_unique="1")
    assert out["ok"] is True and out["stashed"] is True
    data = json.loads(f_text(agent._sidecar_path(f)))
    assert data["revertStash"][0]["content"] == "unsaved work\n"
    assert data["revertStash"][0]["version_id"] == "sess-a@v1"
    # ...and the keys this module does not own survive the read-merge-write.
    assert isinstance(data["claudeSessions"], list)


def test_content_in_no_checkpoint_is_not_destroyed_without_a_confirmation(
    agent, claude_home, tmp_path
):
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "sess-a", f, "wanted\n")
    out = agent.main(action="snapshot_revert", file=f, version_id="sess-a@v1")
    assert "error" in out
    assert out["plan"]["unique_current"] is True
    assert f_text(f) == "unsaved work\n"   # nothing written


@skip_root
def test_an_unwritable_target_answers_with_a_reason_not_an_exception(
    agent, claude_home, tmp_path
):
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "sess-a", f, "old\n")
    os.chmod(f, 0o444)
    try:
        plan = agent.main(action="snapshot_plan", file=f,
                          version_id="sess-a@v1")
        assert plan["ok"] is False and plan["error"]
        out = agent.main(action="snapshot_revert", file=f,
                         version_id="sess-a@v1", confirm_unique="1")
        assert "error" in out
        # The refusal must land BEFORE the stash: a failed revert that still
        # mutated the sidecar is the hazard this ordering exists for.
        assert not os.path.exists(agent._sidecar_path(f))
        assert f_text(f) == "disk\n"
    finally:
        os.chmod(f, 0o644)


def test_a_directory_cannot_be_planned_or_reverted(agent, claude_home, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    assert "error" in agent.main(action="snapshot_plan", file=str(folder),
                                 version_id="sess-a@v1")
    assert "error" in agent.main(action="snapshot_revert", file=str(folder),
                                 version_id="sess-a@v1")


def test_a_mount_backed_target_is_refused_before_anything_stats_it(
    agent, claude_home, tmp_path
):
    # Every action on this panel refuses a path under the mounts dir: the bytes
    # come from a remote over FUSE and a wedged mount turns an ordinary kernel
    # stat into a hang. Same answer condition.py gives, so this is a state the
    # page cannot reach — the module is the guarantee (MD-11).
    mounted = tmp_path / "home" / "mounts" / "src" / "notes.md"
    mounted.parent.mkdir(parents=True)
    mounted.write_text("remote\n")
    for action in ("snapshots", "snapshot_plan", "snapshot_revert"):
        got = agent.main(action=action, file=str(mounted),
                         version_id="sess-a@v1")
        assert "error" in got, action
        assert "mount" in got["error"]


def test_the_store_is_never_written_by_a_revert(agent, claude_home, tmp_path):
    # Still strictly read-only: the revert writes the TARGET and the sidecar,
    # never Claude Code's own edit history.
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "sess-a", f, "old\n")
    root = claude_home / "file-history"
    snap = lambda: sorted(  # noqa: E731
        (os.path.relpath(os.path.join(d, n), root),
         os.path.getsize(os.path.join(d, n)))
        for d, _, fs in os.walk(root) for n in fs)
    before = snap()
    agent.main(action="snapshot_revert", file=f, version_id="sess-a@v1",
               confirm_unique="1")
    assert snap() == before


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


def test_a_row_can_be_opened_and_gone_back_to(source):
    # "There is no point in just showing the snapshots list" — a row expands to
    # its diff and carries the one action that makes the panel mean anything.
    assert 'action: "snapshot_plan"' in source
    assert 'action: "snapshot_revert"' in source
    assert "Go back to this snapshot" in source


def test_the_page_never_reverts_off_an_id_it_chose_itself(source):
    # The write is only ever handed the id the PLAN returned — the same
    # freshness contract the module enforces, so the page cannot be the caller
    # that breaks it.
    body = source[source.index("async function snapGoBack"):]
    body = body[: body.index("\n}\n")]
    assert "plan.id" in body
    assert "v.id" not in body


def test_a_refusal_is_shown_in_the_row_rather_than_hiding_it(source):
    # writable_reason / plan refusals are ordinary states of a file: the row
    # stays, says why, and the button goes away — a vanished row reads as a bug.
    body = source[source.index("function snapExpand"):]
    assert "plan.error" in body


def test_the_landing_page_always_loads_the_snapshots_panel(source):
    # Both paths onto the landing page — boot with no session, and "Back to
    # chats" — must build it, or a resumed chat can never see the panel.
    back = source[source.index('document.getElementById("back").onclick'):]
    back = back[: back.index("\n};")]
    assert "loadSnapshots()" in back


def test_the_boot_call_does_not_enrich(source):
    # Enrichment reads session transcripts (5 MB+). The boot timeline must not
    # pay that on every file open — same rule the annotate panel followed.
    call = source[source.index('action: "snapshots"'):]
    call = call[: call.index(")")]
    assert "enrich" not in call
