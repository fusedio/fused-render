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


def test_the_panel_is_only_offered_for_a_file_target(source):
    # `targetNoun` is the template's own file/folder/project answer (set by
    # paneURL from the stat), so the panel hangs off that rather than a second
    # kind probe that could disagree with it. NOT `paneNoun`, which names the
    # left pane's document ("preview"/"app") and is never "file".
    body = source[source.index("async function mountSnapshots"):]
    body = body[: body.index("\n}")]
    assert 'targetNoun !== "file"' in body
    assert 'paneNoun !==' not in body
    # And it must WAIT for that answer: both boot IIFEs start together, so an
    # unordered read gets "" and the panel never shows.
    assert "await paneReady" in body


def test_the_panel_lists_its_versions_on_arrival(source):
    # The list was behind a click for one revision, to save a round trip through
    # the worker. It saved 0.2 ms of it (`_scan`'s enumeration) and the click cost
    # the user the whole feature on first sight — so mounting the section now
    # fills it, and what got cheap enough to make that fine is the read itself
    # (deltas="0", the assertion two tests below).
    mount = source[source.index("async function mountSnapshots"):]
    mount = mount[: mount.index("\n}")]
    assert "loadSnapshots()" in mount
    # Cached across a trip into the chat and back: same file, same timeline, so
    # the second mount repaints instead of re-asking.
    assert "if (snapLoaded && snapTimeline) { renderSnapshots(snapTimeline); return; }" in mount


def test_no_control_opens_the_section_because_nothing_is_closed(source):
    # Two revisions were spent on the wrong element: the heading as a toggle (a
    # bordered full-width pill wearing 11px uppercase letter-spaced micro-type —
    # "the claude snapshots toggle thing UI is very ugly"), then a placeholder
    # .snap-row with a "hide" beside the label. Both are gone with the deferral
    # that justified them; the heading is a label over a stack of rows, exactly
    # like #recent's .head over .chat-row.
    assert '<button id="snapstoggle"' not in source
    assert 'id="snapsopen"' not in source
    assert 'id="snapshide"' not in source
    assert "Show version history" not in source
    assert "snapShow" not in source
    assert "snapExpanded" not in source
    assert 'class="head" id="snapshead"' in source
    # The body is no longer something to unhide.
    assert '<div id="snapsbody">' in source


def test_the_rows_are_grouped_into_per_session_runs(source):
    # Version numbers restart in every session (FH-4), so a flat merged list shows
    # "v2" once per chat that edited the file and reads as duplicate rows — "why do
    # we have multiple snapshots of the same version". The number is real and
    # per-chain, so the chain is drawn.
    runs = source[source.index("function snapRuns(versions)"):]
    runs = runs[: runs.index("\n}")]
    # CONTIGUOUS runs, not a group-by: `_locate` walks the merged timeline
    # positionally, so this may only insert boundaries — never reorder or merge.
    assert "sort" not in runs
    assert "open.session === v.session" in runs
    render = source[source.index("function renderSnapshots(timeline)"):]
    render = render[: render.index("\n}")]
    assert "for (const run of snapRuns(timeline.versions))" in render
    assert "snapRunHead(run)" in render
    # The rows themselves are unchanged and still carry their own identity.
    assert "snapItem(v, timeline)" in render


def test_a_run_is_named_by_the_chat_it_came_from_when_that_is_known(source):
    # The name is the SAME string "Recent chats" labels its rows with, so the two
    # sections of the landing page agree about what a chat is called. That string
    # is `sessionTitle(s)` and no longer the raw `s.preview`: a preview whose
    # message carried a screenshot or notes STARTS with a machine-written wire
    # block, so the unprocessed field named chats "<pane-shot> The user attached a
    # pi…". The invariant this test exists for is untouched — both sections still
    # read one writer — and that is exactly why it is the writer that moved.
    recent = source[source.index("async function loadRecent"):]
    recent = recent[: recent.index("\n}")]
    assert "const title = sessionTitle(s);" in recent
    assert "snapNames.set(s.id, title)" in recent
    # Populated BEFORE the no-sessions early return: a file can have chains in the
    # store and nothing in its sidecar, and vice versa.
    assert recent.index("snapNames.set") < recent.index("if (!sessions.length)")
    assert 'row.querySelector(".row-title").textContent = sessionTitle(s);' in source

    # The two reads RACE, so naming cannot depend on which lands first. "Back to
    # chats" fires loadRecent and mountSnapshots together, and on a page opened
    # straight into a resumed chat (?session_id=) that path is the panel's FIRST
    # read — so if the snapshots round trip wins, the headings paint unnamed and
    # nothing else would ever come back to fix them.
    assert "if (named && snapTimeline) renderSnapshots(snapTimeline);" in recent
    # Guarded on a real change, so the repaint is not spent on every landing.
    assert "snapNames.get(s.id) !== title" in recent

    head = source[source.index("function snapRunHead(run)"):]
    head = head[: head.index("\n}")]
    # A miss is ordinary, not a bug: the store records every Claude Code session
    # that touched the file, including terminal ones that were never in the
    # sidecar. Fallback says only what is certain, and the short id is what
    # distinguishes two unnamed chains.
    assert 'name.textContent = known || "chat";' in head
    assert 'run.session.slice(0, 8)' in head
    # The full session id stays reachable.
    assert 'el.title = "session " + run.session;' in head
    # Singular for one, plural for more — a "1 checkpoints" heading is the kind of
    # thing that survives forever once shipped.
    assert '(n === 1 ? " checkpoint" : " checkpoints")' in head


def test_a_folder_target_costs_nothing_at_the_end_of_a_turn(source):
    # `snapInvalidate` fires when EVERY turn ends, including in a folder chat
    # where `mountSnapshots` returned without revealing anything. The hidden
    # section would swallow the render, but the round trip would still be spent
    # and agent.py would still refuse a folder, once per turn, for ever.
    mount = source[source.index("async function mountSnapshots"):]
    mount = mount[: mount.index("\n}")]
    assert "snapMounted = true;" in mount
    inv = source[source.index("function snapInvalidate() {"):]
    inv = inv[: inv.index("\n}")]
    assert "if (!snapMounted) return;" in inv


def test_the_wait_is_a_note_and_the_failure_is_a_retry(source):
    body = source[source.index("async function loadSnapshots"):]
    body = body[: body.index("\n}")]
    # FIRST paint only: a refetch keeps the rows it has, because swapping a good
    # list for "reading…" reads as the panel losing the history.
    assert 'if (!list.firstChild) list.append(snapNote("reading the version history…"));' in body
    ok, err = body.split("} catch", 1)
    # The failure is actionable rather than terminal: the note says what went
    # wrong and the heading grows the way to ask again.
    assert "snapRetryBtn.hidden = false;" in err
    assert "snapRetryBtn.hidden = true;" in ok
    assert '<button id="snapsretry" type="button" hidden>' in source
    css = source[source.index("  #snapsretry {"):]
    css = css[: css.index("\n  }")]
    assert "border: 0;" in css
    assert "color: var(--faint);" in css
    assert "text-transform: none;" in css


def test_a_loaded_timeline_is_cached_until_something_appends_to_the_chain(source):
    # Re-opening the section must not re-ask. Only a successful read arms the
    # cache — a failure has to be retryable on the next expand.
    body = source[source.index("async function loadSnapshots"):]
    body = body[: body.index("\n}")]
    assert "snapLoaded = true;" in body
    ok, err = body.split("} catch", 1)
    assert "snapLoaded = true;" in ok
    assert "snapLoaded =" not in err
    # A retry after a failure must actually re-ask — `snapLoaded` false is what
    # allows it, and the button goes straight at the read.
    assert "snapRetryBtn.onclick = () => loadSnapshots();" in source
    # And a revert, which appends to the chain, repaints from the timeline the
    # write returned rather than trusting the cached one.
    revert = source[source.index("async function snapGoBack"):]
    revert = revert[: revert.index("\n}\n")]
    assert "renderSnapshots(out.timeline)" in revert
    assert "loadSnapshots()" in revert


def test_a_finished_chat_turn_drops_the_cached_timeline(source):
    # A revert is not the only thing that appends to this chain: Claude edits the
    # file in the SAME page's chat and Claude Code checkpoints what it edits. The
    # cache is keyed to nothing but the page, so without this a landing page
    # reached after a turn showed the pre-turn position, stale deltas and none of
    # the new versions.
    inv = source[source.index("function snapInvalidate() {"):]
    inv = inv[: inv.index("\n}")]
    assert "snapLoaded = false;" in inv
    # And repaints NOW rather than only dropping the cache: the list is on screen
    # from the moment the panel mounts, so leaving a stale one up is worse than
    # spending the round trip — the same argument the revert path makes.
    assert "loadSnapshots();" in inv

    # Hooked at every place a run ENDS, beside annResolveSent — the template's
    # existing "that turn is over" moment. Both: the poll loop's own `done`
    # branch, and resumeRun's, for a turn this frame was not attached to.
    ends = [i for i in range(len(source))
            if source.startswith("annResolveSent();", i)]
    assert len(ends) >= 2
    hooked = sum(1 for i in ends if "snapInvalidate();" in source[i: i + 400])
    assert hooked >= 2, "a run can end without the snapshots cache being dropped"


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


def test_the_landing_page_always_offers_the_snapshots_panel(source):
    # Both paths onto the landing page — boot with no session, and "Back to
    # chats" — must offer it, or a resumed chat can never see the panel.
    back = source[source.index('document.getElementById("back").onclick'):]
    back = back[: back.index("\n};")]
    assert "mountSnapshots()" in back
    boot = source[source.index("// ── boot: resume from URL"):]
    assert "mountSnapshots()" in boot


def test_the_list_read_declines_both_costs(source):
    # Enrichment reads session transcripts (5 MB+) — same rule the annotate panel
    # followed. The exact deltas are `difflib` per version, which is ~99% of the
    # read (290 ms of 292 on a 453 KB file with 12 checkpoints) for two numbers on
    # a row; declining it is what makes a list-on-arrival panel honest, and the
    # real diff still arrives per-row from `snapshot_plan`.
    call = source[source.index('action: "snapshots"'):]
    call = call[: call.index(")")]
    assert "enrich" not in call
    assert 'deltas: "0"' in call


def test_an_inexact_delta_renders_as_one_net_term_not_a_pair(source):
    # An exact delta is a PAIR (difflib counted both directions). A net is a
    # SINGLE number by construction — `_delta`'s cheap branch is max(0, ver−cur)
    # against max(0, cur−ver), so at most one side is ever non-zero. Rendering it
    # as a pair put "~+0 −43" down every row of a file that has only grown, where
    # the "+0" is the shape of the arithmetic and not a measurement. Caught by
    # looking at the running app, not by a test — hence this one.
    body = source[source.index("function snapDelta(v)"):]
    body = body[: body.index("\n}")]
    assert 'if (!v.removed) return span("snap-plus", "~+" + v.added);' in body
    assert 'if (!v.added) return span("snap-minus", "~−" + v.removed);' in body
    # Same line COUNT, different bytes: the net has nothing to report, so it says
    # the one thing it does establish.
    assert 'if (!v.added && !v.removed) return document.createTextNode("changed");' in body
    # All three only reachable for a row that DOES differ — "on disk now" is
    # decided first, so an identical version never says "changed".
    assert body.index('"on disk now"') < body.index('"changed"')
    # The exact pair is still the exact pair.
    assert 'span("snap-plus", (v.exact ? "" : "~") + "+" + v.added)' in body
