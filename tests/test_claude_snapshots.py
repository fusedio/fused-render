"""Claude Code's file-history snapshots, shown by the Claude chat (SPEC
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
  action refuses and the chat never renders the section.

And the panel is INTERACTIVE: a row can be gone back to. That half is
`snapshot_plan` + `snapshot_revert`, the same two-call contract annotate pinned
(SPEC §34, D194) — the plan chooses and describes, the write only applies an id
the plan already handed out.
"""
import importlib.util
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
    # The mounts dir hangs off home_dir(), which is how the mount-backed
    # refusal below gets a path to point at — pin FUSED_RENDER_HOME so these
    # tests never touch a real ~/.fused-render.
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


def test_two_sessions_both_number_from_v1_and_the_order_stays_temporal(
    agent, claude_home, tmp_path
):
    # The data behind the panel's grouping, and the reason a flat list looked like
    # duplicates: each session's chain restarts at v1, so "v1" appears once per
    # session. The merged order is by TIME (never by N), which is what the page
    # groups into contiguous runs without reordering.
    target = tmp_path / "notes.md"
    target.write_text("now\n")
    write_version(claude_home, "sess-old", str(target), "old one\n", mtime=1000)
    write_version(claude_home, "sess-old", str(target), "old two\n", mtime=2000)
    write_version(claude_home, "sess-new", str(target), "new one\n", mtime=3000)

    got = agent.main(action="snapshots", file=str(target), deltas="0")
    rows = [(v["session"], v["version"]) for v in got["versions"]]
    assert rows == [("sess-new", 1), ("sess-old", 2), ("sess-old", 1)]
    # Both sessions have a v1, and they are different rows.
    assert sum(1 for _s, n in rows if n == 1) == 2
    # Newest first, strictly by mtime.
    times = [v["mtime"] for v in got["versions"]]
    assert times == sorted(times, reverse=True)


def test_declining_the_deltas_changes_the_counts_and_nothing_else(
    agent, claude_home, tmp_path
):
    # The page asks for `deltas="0"` on every file open, so this is the shape the
    # panel actually renders. It must be the SAME timeline: `position`, `revert`,
    # `offer` and every row's `differs` come off a byte comparison, and only the
    # added/removed pair softens to net line counts with `exact: False` beside it.
    target = tmp_path / "notes.md"
    target.write_text("a\nb\nc\n")
    write_version(claude_home, "sess-a", str(target), "a\nB\nc\n", mtime=1000)
    write_version(claude_home, "sess-a", str(target), "a\nb\nc\nd\n", mtime=2000)

    exact = agent.main(action="snapshots", file=str(target))
    cheap = agent.main(action="snapshots", file=str(target), deltas="0")

    structural = ("position", "revert", "offer", "at_earliest", "unique_current")
    assert {k: exact[k] for k in structural} == {k: cheap[k] for k in structural}
    assert [(v["id"], v["differs"]) for v in exact["versions"]] \
        == [(v["id"], v["differs"]) for v in cheap["versions"]]
    assert all(v["exact"] for v in exact["versions"])
    assert not any(v["exact"] for v in cheap["versions"])
    # v2 adds a line: net counts get that one right.
    assert (cheap["versions"][0]["added"], cheap["versions"][0]["removed"]) == (1, 0)
    # v1 changes a line in place — the honest net answer is 0/0, which is exactly
    # why the page renders an inexact zero pair as the word "changed" rather than
    # as "~+0 −0" next to a row it also calls different from disk.
    assert (cheap["versions"][1]["added"], cheap["versions"][1]["removed"]) == (0, 0)
    assert cheap["versions"][1]["differs"] is True


def test_the_deltas_knob_defaults_to_the_complete_answer(agent, claude_home, tmp_path):
    # Opposite direction to `enrich`, on purpose: absent means EXACT here, because
    # a hand-written call that did not think about it should get the whole truth,
    # and only an explicit "0"/"false" declines.
    target = tmp_path / "notes.md"
    target.write_text("a\nb\n")
    write_version(claude_home, "sess-a", str(target), "a\nB\n", mtime=1000)
    for value in ("", "1", "true", "yes"):
        got = agent.main(action="snapshots", file=str(target), deltas=value)
        assert all(v["exact"] for v in got["versions"]), value
    for value in ("0", "false"):
        got = agent.main(action="snapshots", file=str(target), deltas=value)
        assert not any(v["exact"] for v in got["versions"]), value


def test_a_plan_is_always_exact_however_the_list_was_read(
    agent, claude_home, tmp_path
):
    # The list may soften its counts; the thing a user CONFIRMS may not. The plan
    # has no `deltas` parameter at all — same discipline as its always-on enrich.
    target = tmp_path / "notes.md"
    target.write_text("a\nb\nc\n")
    write_version(claude_home, "sess-a", str(target), "a\nB\nc\n", mtime=1000)
    listed = agent.main(action="snapshots", file=str(target), deltas="0")
    plan = agent.main(action="snapshot_plan", file=str(target),
                      version_id=listed["versions"][0]["id"])
    assert plan["ok"] is True
    assert plan["exact"] is True
    assert (plan["added"], plan["removed"]) == (1, 1)
    assert plan["diff"]["lines"]


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


# ----------------------------------------- getting the chain written at all


class _HostProc:
    """Stands in for the session_host.py process `_start` now Popens instead
    of the CLI itself; a fake stdin is enough here since only the Popen
    kwargs (the child env) are under test, not the CLI's own argv."""
    pid = 4242

    class _Stdin:
        def write(self, data):
            pass

        def close(self):
            pass

    def __init__(self):
        self.stdin = _HostProc._Stdin()


def _spawn_kw(agent, monkeypatch, target, tmp_path):
    """The Popen kwargs of one run — for asserting on the child ENV rather
    than the CLI flags. Mirrors the helper in test_fused_cli_export.py."""
    seen = {}

    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("kw", kw),
                                           _HostProc())[1])
    out = agent._start(str(target), "hi", "", "", "")
    assert "error" not in out, out
    return seen["kw"]


def test_a_headless_run_asks_for_the_checkpoints_the_panel_reads(
        agent, tmp_path, monkeypatch):
    """Without this the panel is structurally empty for its own chat's work.

    Checkpointing is OFF by default in a non-interactive session, and every run
    this module spawns is `-p`. So the store only ever held versions written by
    a TERMINAL claude in that folder, and a file this chat had just edited four
    times reported "Claude has no recorded versions of this file" — the one
    question the panel exists to answer. Asked for in the ENV, not in
    `--settings`: the CLI takes a separate branch when `isInteractive()` is
    false and that branch reads only the two env vars (D394)."""
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
                       raising=False)
    target = tmp_path / "notes.md"
    target.write_text("# now\n")
    kw = _spawn_kw(agent, monkeypatch, target, tmp_path)
    assert kw["env"]["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] == "1"


def test_a_user_who_set_it_themselves_keeps_their_value(
        agent, tmp_path, monkeypatch):
    """setdefault, not assignment. The CLI coerces the value (`1/true/yes/on`,
    everything else false), so an explicit `=0` is a real opt-out and must not
    be overwritten by ours — this template does not get to re-enable a
    checkpoint store the user turned off."""
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "0")
    target = tmp_path / "notes.md"
    target.write_text("# now\n")
    kw = _spawn_kw(agent, monkeypatch, target, tmp_path)
    assert kw["env"]["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] == "0"


# --------------------------------------------------- going back to a snapshot


def _target(tmp_path, content, name="notes.md"):
    # `newline=""` disables universal-newline translation — same fix, same
    # reason, as _claude_history.write_version's own comment: file_history.py
    # compares this file's on-disk bytes against a checkpoint's bytes exactly
    # (`differs` is a byte comparison, by design — see file_history._locate),
    # and `Path.write_text`'s default text mode would silently inflate every
    # "\n" here to "\r\n" on Windows, making a target deliberately written to
    # equal a checkpoint's content ("disk\n" == "disk\n") no longer equal it
    # on disk, and derailing the positional walk that test is asserting about.
    f = tmp_path / name
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
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
    # Still strictly read-only: the revert writes the TARGET,
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
