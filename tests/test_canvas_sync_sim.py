"""Canvas sync simulator (sync revision contract §4).

A deterministic model-based test over the REAL `_SyncManager`: the fused CLI
and its shims are answered in process by a FakeServer (tests/canvas_sim),
the watcher's threads are replaced by a scheduler that runs one loop pass
per `tick`, and three actors — the embedded frame, a Claude session in the
clone, a human in the hosted tab — interleave under a seeded RNG.

After every run the world is driven to quiescence and four invariants are
checked:
  (1) convergence  disk == server == frame
  (2) no lost write every accepted write is final, or was knowingly
                    superseded by a later write to the same key, or lost a
                    concurrent tie (counted)
  (3) no wedge     no server_stale / held / error state once all are idle
  (4) reasons      every refusal / 409 / 412 / stale chip names what it is about

`FUSED_RENDER_SIM_SEEDS=<n>` widens the seed count (default 200 in CI's
budget), `FUSED_RENDER_SIM_SEED=<k>` replays one seed, and
`FUSED_RENDER_SIM_STEPS` changes the run length.

The eight ledger traces (design page §2) are the named tests below.
"""
from __future__ import annotations

import os
import random
import time
import tomllib

import pytest

import fused_render.canvases as canvases_mod
from fused_render import canvas_snapshot
from fused_render.fusedcli import FusedCli

from canvas_sim.server import SimProcs
from canvas_sim.world import (ABSENT, FakeAgent, Ledger, World, body_text, make_row, token_of,
                              write_file)
from test_canvases import _reset_module_state, harness  # noqa: F401 — fixtures by name

SEEDS = int(os.environ.get("FUSED_RENDER_SIM_SEEDS", "200"))
STEPS = int(os.environ.get("FUSED_RENDER_SIM_STEPS", "30"))


@pytest.fixture
def sim(harness, monkeypatch):  # noqa: F811 — the imported fixture
    """Wire the fakes: the subprocess name inside canvases.py, an in-process
    (non-external) CLI so every shim path is taken, no watcher/fs threads,
    zero debounce/poll/liveness-cache, a fake agent module."""
    harness.log_in()
    procs = SimProcs()
    agent = FakeAgent()
    monkeypatch.setattr(canvases_mod, "subprocess", procs)
    monkeypatch.setattr(canvases_mod, "fused_cli",
                        lambda: FusedCli(command=["SIMFUSED"], external=False))
    monkeypatch.setattr(canvases_mod._SyncManager, "_run", lambda self: None)
    monkeypatch.setattr(canvases_mod._SyncManager, "_watch_fs", lambda self: None)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.0)
    monkeypatch.setattr(canvases_mod, "AGENT_LIVE_CACHE_S", 0.0)
    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: agent)
    monkeypatch.setattr(canvases_mod, "_kick_workbench_skills", lambda: None)
    monkeypatch.setattr(canvases_mod, "_lease_owner", lambda: "fused-render:sim@host",
                        raising=False)

    class Sim:
        def __init__(self):
            self.harness, self.procs, self.agent = harness, procs, agent

        def world(self, name="alpha", **kw) -> World:
            return World(harness, procs, agent, name, **kw)

    return Sim()


def _events(w: World) -> list[dict]:
    return w.client.get(f"/api/canvases/sync/log?env={w.env}&name={w.name}").json()["events"]


def _actions(w: World) -> list[str]:
    return [e["action"] for e in _events(w)]


def _assert_invariants(w: World) -> None:
    problems = w.check_all()
    assert not problems, w.report(problems)


# -- the checker itself ---------------------------------------------------------


def _deletes_then_final(history: list[tuple]) -> tuple[list[str], int]:
    """A ledger from (actor, value, observed, seen) rows on `body:a`, judged
    against a final value of "C" (the node still there)."""
    led = Ledger()
    led.write("init", "body:a", "A", None)
    for step, (actor, value, observed, seen) in enumerate(history, 1):
        led.step = step
        led.write(actor, "body:a", value, observed, seen=seen)
    return led.lost_writes({"body:a": "C"})


def test_ledger_a_delete_that_saw_the_history_is_reverted_not_a_tie():
    """Session writes B then C; the human loads C and deletes. A final C is
    the delete reverted: both session writes are in the deleter's observed
    history, so nothing raced it (CodeRabbit on `_delete_raced`)."""
    problems, ties = _deletes_then_final([
        ("session", "B", "A", None),
        ("session", "C", "B", None),
        ("human", "del_h1", "C", {"body:a": "C"}),
    ])
    assert ties == 0
    assert problems and "a reverted write" in problems[0], problems


def test_ledger_a_delete_that_raced_an_unseen_edit_is_a_tie():
    """The human loaded B and deleted while the session wrote C over B: the
    delete never saw C, so either outcome is a tie, not a lost write."""
    problems, ties = _deletes_then_final([
        ("session", "B", "A", None),
        ("human", "del_h1", "B", {"body:a": "B"}),
        ("session", "C", "B", None),
    ])
    assert problems == [] and ties == 1


def test_ledger_absent_final_needs_a_delete_head():
    led = Ledger()
    led.write("init", "body:a", "A", None)
    led.write("session", "body:a", "B", "A")
    problems, _ties = led.lost_writes({"body:a": ABSENT})
    assert problems and "no delete is a head" in problems[0]


def test_convergence_fails_when_the_frame_shows_a_node_without_its_text(sim):
    """A displayed source node the frame holds no body for is not converged
    (CodeRabbit on `check_convergence`): it used to be skipped."""
    w = sim.world()
    assert w.quiesce() and w.check_convergence() == []
    del w.frame.bodies["a"]
    assert "body:a the frame shows the node without its source text" in w.check_convergence()


def test_convergence_compares_the_frames_whole_text_not_just_its_token(sim):
    """Same token, different text (CodeRabbit on `check_convergence`): the
    frame does not show what disk holds, so the run has not converged."""
    w = sim.world()
    assert w.quiesce() and w.check_convergence() == []
    w.frame.bodies["a"] += "# a trailing line only the frame has\n"
    assert any(p.startswith("body:a frame") for p in w.check_convergence())


# -- the eight ledger traces (design page §2) -----------------------------------


def _text(w: World, file: str) -> bytes:
    with open(os.path.join(w.dir, file), "rb") as fh:
        return fh.read()


def _server_tok(w: World, slug: str) -> str:
    return token_of(w.server.udfs[slug].text)


def test_trace_1_full_rehydrate_after_push(sim):
    """Incident 1, full re-hydrate after push: the post-push probe answers the
    pre-import state once; the next poll must not read the push's own write
    as a server move (no stale chip, no merge, no re-push, no full delta)."""
    w = sim.world()
    w.server.stale_probe_after_import = True
    w.session.start_turn()
    w.session.edit_body("a", "tk_s1")
    w.follow()
    w.session.end_turn()
    for _ in range(6):
        w.tick()
        w.follow()
    w.frame.apply_inbox()
    assert w.server.imports == 1 and _server_tok(w, "a") == "tk_s1"
    acts = _actions(w)
    assert "server merged" not in acts and "probe stale" not in acts, acts
    assert not w.stale_seen, w.stale_seen
    assert w.manager.push_seq == 1
    later = w.frame.deltas[1:]
    assert not [d for d in later if d.get("full")], later
    upserted = {o["name"] for d in later for o in d["ops"]
                if o["op"] == "upsert" and set(o) - {"op", "name", "file", "id"}}
    assert upserted <= {"a"}, upserted
    _assert_invariants(w)


def test_trace_2_nodeless_push_waits_for_the_turn(sim):
    """Incident 2, node-less push: a session's half-done change set (a source
    file whose canvas.toml row is not written yet) is never published mid-turn;
    the turn end publishes file and row together."""
    w = sim.world()
    w.session.start_turn()
    path = os.path.join(w.dir, "s1.py")
    write_file(path, body_text("s1.py", "tk_s1"))
    w.ledger.write("session", "body:s1", "tk_s1", None)
    w.names.add("s1")
    w.follow()
    w.tick(4)
    assert w.server.imports == 0, w.server.import_log
    doc = w.session._doc()
    doc["canvas"]["nodes"].append(make_row("s1", 4000, "tk_st1"))
    w.ledger.write("session", "x:s1", 4000, None)
    w.ledger.write("session", "title:s1", "tk_st1", None)
    w.session._write_doc(doc)
    w.follow()
    w.tick(3)
    assert w.server.imports == 0, "published while the turn was live"
    w.session.end_turn()
    w.tick(4)
    assert "s1" in w.server.udfs and "s1" in w.server.rows()
    for imp in w.server.import_log:
        assert set(imp["files"]) <= set(imp["rows"]), f"a node-less file was pushed: {imp}"
    _assert_invariants(w)


def test_trace_3_two_watchers_one_canvas(sim, monkeypatch):
    """Incident 3, two watchers: two fused-renders sync one canvas (here two
    clones of the same server canvas). The lease names the first; the second
    holds its pushes with a reason naming the owner, and once the first lets
    go the second publishes without losing either side's edit. (Two processes
    on ONE clone folder are refused by the pid flock: test_canvas_sync_watcher
    covers that seam; one process cannot hold two flocks against itself.)"""
    monkeypatch.setattr(canvases_mod, "LEASE_RENEW_S", 0.0, raising=False)
    a = sim.world("alpha")
    monkeypatch.setattr(canvases_mod, "_lease_owner", lambda: "fused-render:other@laptop",
                        raising=False)
    b = sim.world("alpha", env="prod", server=a.server)
    assert a.server.lease_owner == "fused-render:sim@host"
    b.session.start_turn()
    b.session.edit_body("b", "tk_sb")
    b.session.end_turn()
    b.tick(4)
    assert _server_tok(b, "b") == "tk_init_b", "the second watcher pushed over the lease"
    d2s = b.state()["legs"]["disk_to_server"]
    assert d2s["state"] == "held" and "fused-render:sim@host" in (d2s["reason"] or ""), d2s
    a.session.start_turn()
    a.session.edit_body("a", "tk_sa")
    a.session.end_turn()
    a.tick(4)
    assert _server_tok(a, "a") == "tk_sa"
    a.stop()  # releases the lease
    assert a.server.lease_owner is None
    assert b.quiesce()
    assert _server_tok(b, "b") == "tk_sb" and _server_tok(b, "a") == "tk_sa"
    assert token_of(_text(b, "a.py").decode()) == "tk_sa"
    assert b.check_wedge() == []


def test_trace_4_folder_grammar_on_pull(sim):
    """Incident 4, folder grammar on pull (minimal): a folder the hosted tab
    made around two nodes reaches the clone by the automatic merge as one
    well-formed folder row, and the frame shows it."""
    w = sim.world()
    status, _ = w.server.hosted_folder("F", ["a", "b"], if_match=w.human.baseline)
    assert status == 200
    w.tick(4)
    w.follow()
    w.frame.apply_inbox()
    with open(os.path.join(w.dir, "canvas.toml"), "rb") as fh:
        doc = tomllib.load(fh)
    rows = canvas_snapshot.rows_of_doc(doc)["nodes"]
    assert rows.get("F", {}).get("type") == "udf-folder", rows
    assert rows["F"].get("childUdfOrder") == ["a", "b"]
    assert canvas_snapshot.folder_problems(doc) == []
    assert w.manager.server_stale is None
    assert "F" in w.frame.nodes
    _assert_invariants(w)


def test_trace_5_lock_owned_after_update(sim):
    """Incident 5, lock owned after update: a pre-update fused-render's flock
    outlived the app swap. Not modelable in one process (the flock is per
    process and this process re-takes its own lock by design)."""
    pytest.skip("cross-process flock after an app update: covered by "
                "test_canvas_sync_watcher's lock tests and the self-port guard (PR #2024)")


@pytest.mark.parametrize("revision_mode", [True, False], ids=["revision", "old_server"])
def test_trace_6_delete_wedge_and_silent_rollback(sim, revision_mode):
    """Incident 6, delete wedge + silent rollback: the hosted tab deletes a
    node the clone holds an unpushed layout edit on. The merge must neither
    roll back silently nor wedge: it settles with every leg idle and one
    canvas on disk, server and frame."""
    w = sim.world(revision_mode=revision_mode)
    w.frame.drag("n", 5555)
    w.frame.deliver()
    w.follow()
    w.human.delete("n", "del_h1")
    w.tick(6)
    rollbacks = [e for e in _events(w) if e["action"] == "pull rollback"]
    assert w.manager.merge_rollback_seq == 0 or all(e["detail"] for e in rollbacks)
    _assert_invariants(w)


def test_trace_7_coedit_r2_drag_does_not_revert_claude_keys(sim):
    """Incident 7 / co-edit R2, drag reverts Claude's keys: Claude retitles a
    node, the page has cut the delta but the frame serialised a drag before
    applying it. The stale snapshot bounces; the drag lands and the title
    stays Claude's."""
    w = sim.world()
    w.session.start_turn()
    w.session.edit_row("a", "title", "tk_s_title")
    w.follow()
    assert w.frame.inbox, "the page did not cut a delta for Claude's write"
    held, w.frame.inbox = w.frame.inbox, []
    w.frame.drag("a", 4242)          # serialised on the old rev
    w.frame.inbox = held
    w.frame.deliver()                # the delta arrives first; the snapshot is stale
    w.session.end_turn()
    assert w.quiesce()
    rows, _files = w.disk_view()
    assert rows["a"]["title"] == "tk_s_title" and rows["a"]["x"] == 4242, rows["a"]
    assert "snapshot bounced" in _actions(w)
    _assert_invariants(w)


def test_trace_7_coedit_r6_no_ghost_node_after_delete(sim):
    """Incident 7 / co-edit R6, ghost node after delete: the user deletes a
    node in the frame during a live turn; Claude then writes that node's file
    back (an edit it started before). The node stays off the canvas: no row,
    not in the frame, not on the server, and Claude is told."""
    w = sim.world()
    w.session.start_turn()
    w.session._hook(os.path.join(w.dir, "a.py"))  # the session's first tool call
    w.frame.delete("n", "del_f1")
    w.frame.deliver()
    w.follow()
    path = os.path.join(w.dir, "n.md")
    assert not os.path.exists(path)
    write_file(path, body_text("n.md", "tk_s_late"))
    w.session._hook(path)
    w.follow()
    w.session.end_turn()
    w.quiesce()
    rows, _files = w.disk_view()
    assert "n" not in rows and "n" not in w.frame.nodes
    assert "n" not in w.server.udfs and "n" not in w.server.rows()
    assert any("deleted node `n`" in c for c in w.session.contexts), w.session.contexts
    assert w.check_wedge() == []


@pytest.mark.parametrize("revision_mode", [True, False], ids=["revision", "old_server"])
def test_trace_8_hls_tiff_hash_moves_bytes_equal(sim, revision_mode):
    """Incident 8, HLS_Tiff widget_1: the server rewrote a widget's metadata —
    its hash moved, its exported bytes are identical. Byte-equal rebaseline:
    no stale chip, no merge, no file written, nothing held."""
    w = sim.world(revision_mode=revision_mode)
    w.session.start_turn()
    w.session.edit_body("a", "tk_s1")       # an unrelated local edit, turn live
    w.follow()
    before = (_text(w, "w.json"), os.stat(os.path.join(w.dir, "w.json")).st_mtime_ns)
    w.server.rewrite_udf_metadata("w")
    w.tick(4)
    assert w.manager.server_stale is None and not w.stale_seen, w.stale_seen
    assert (_text(w, "w.json"), os.stat(os.path.join(w.dir, "w.json")).st_mtime_ns) == before
    assert "server moved, same bytes, rebaselined" in _actions(w)
    w.session.end_turn()
    w.tick(4)
    assert _server_tok(w, "a") == "tk_s1"
    _assert_invariants(w)


# -- contract §3 behaviours the traces lean on ----------------------------------


def test_first_snapshot_of_a_generation_bounced_keeps_the_drag(sim):
    """Seeded-run finding (co-edit R2 family): a fresh frame (its hello is a
    full delta; no snapshot sent yet) drags a node; before that first
    snapshot lands, a server move is merged and its delta cut. The bounce of
    the generation's FIRST snapshot switches the host's generation, which
    drops the view, so the next delta (one more server move) is a full state
    whose position upserts overwrite the drag the frame has not had acked."""
    w = sim.world()
    w.frame.drag("n", 7777)               # queued, not delivered
    w.human.edit_row("a", "x", 8888)
    w.tick(2)
    w.follow()                            # merged; the page cut the delta
    w.frame.deliver()                     # delta first, then the stale snapshot
    assert "snapshot bounced" in _actions(w)
    w.human.edit_body("b", "tk_h1")       # one more server move before the re-post
    w.tick(2)
    w.follow()
    assert w.quiesce()
    rows, _files = w.disk_view()
    assert (rows["n"]["x"], rows["a"]["x"]) == (7777, 8888), (rows["n"], rows["a"])
    _assert_invariants(w)


def test_cas_push_conflict_merges_and_retries_once(sim):
    """§3 CAS push: a hosted save lands while the push is in flight → 412 →
    merge → one retry with the new revision. Both edits survive."""
    w = sim.world()
    w.session.start_turn()
    w.session.edit_body("a", "tk_s1")
    fired: list = []

    def hosted_save():
        if not fired:
            fired.append(1)
            w.ledger.write("human", "body:b", "tk_h1", "tk_init_b")
            w.server.hosted_save_udf("b", body_text("b.py", "tk_h1"))

    w.procs.during_push.append(hosted_save)
    w.session.end_turn()
    w.tick(6)
    w.procs.during_push.clear()
    assert any("--if-match" in argv for argv in w.procs.kinds("push"))
    assert "push conflict" in _actions(w)
    assert _server_tok(w, "a") == "tk_s1" and _server_tok(w, "b") == "tk_h1"
    assert token_of(_text(w, "b.py").decode()) == "tk_h1"
    _assert_invariants(w)


def test_auto_merge_when_idle_and_held_while_a_run_is_live(sim):
    """§3 auto merge-pull (Q8): a server move merges at once when no run is
    live; while one is, it is held (server_stale with a per-file list, the
    hook tells the session) and merged the moment the turn ends."""
    w = sim.world()
    w.human.edit_body("b", "tk_h1")
    w.tick(3)
    assert token_of(_text(w, "b.py").decode()) == "tk_h1"
    assert "server merged" in _actions(w)
    w.session.start_turn()
    w.session._hook(os.path.join(w.dir, "a.py"))
    w.human.edit_body("a", "tk_h2")
    w.tick(3)
    stale = w.manager.server_stale
    assert stale is not None and stale.get("files"), stale
    assert token_of(_text(w, "a.py").decode()) == "tk_init_a"  # not under a live run
    w.session._hook(os.path.join(w.dir, "b.py"))
    assert any("a.py" in c for c in w.session.contexts), w.session.contexts
    w.session.end_turn()
    w.tick(3)
    assert token_of(_text(w, "a.py").decode()) == "tk_h2"
    _assert_invariants(w)


def test_recreated_canvas_new_id_restarts_revision(sim):
    """Adversarial knob: the canvas was deleted and created again (new id,
    revision back at 1). Not an older copy of the base: the engine
    rebaselines (same bytes) and keeps publishing."""
    w = sim.world()
    w.tick(2)
    w.server.recreate()
    w.tick(4)
    assert w.manager.server_stale is None
    w.session.start_turn()
    w.session.edit_body("a", "tk_s1")
    w.session.end_turn()
    w.tick(4)
    assert _server_tok(w, "a") == "tk_s1"
    _assert_invariants(w)


# -- the seeded interleavings ---------------------------------------------------


def _seeds() -> list[int]:
    one = os.environ.get("FUSED_RENDER_SIM_SEED")
    return [int(one)] if one else list(range(SEEDS))


def test_seeded_interleavings_hold_the_four_invariants(sim):
    """Every seed: STEPS random actor steps, quiescence, the four invariants.
    One seed in four runs against an old server (no `revision`, no lease)."""
    failures: list[tuple[int, str]] = []
    ties = 0
    started = time.time()
    for seed in _seeds():
        w = sim.world(f"s{seed}", revision_mode=seed % 4 != 3, rng=random.Random(seed))
        try:
            if seed % 5 == 4:
                w.server.reserialize_widgets = True
            for _ in range(STEPS):
                w.step()
            problems = w.check_all()
            ties += getattr(w, "tie_losses", 0)
        except Exception as exc:  # noqa: BLE001 — a crash is a failing seed too
            problems = [f"crashed: {type(exc).__name__}: {exc}"]
        report = w.report(problems, tail=40) if problems else ""
        w.stop()
        if problems:
            failures.append((seed, report))
            print(f"seed {seed}: " + " | ".join(p[:160] for p in problems[:3]))
    elapsed = time.time() - started
    print(f"\nsim: {len(_seeds())} seeds x {STEPS} steps in {elapsed:.1f}s, "
          f"{len(failures)} failing, {ties} concurrent-tie losses")
    if failures:
        seed, report = failures[0]
        summary = ", ".join(str(s) for s, _r in failures[:20])
        pytest.fail(f"{len(failures)}/{len(_seeds())} seeds broke an invariant "
                    f"(first: {summary}). Replay with FUSED_RENDER_SIM_SEED={seed}.\n"
                    f"seed {seed}:\n{report}")
