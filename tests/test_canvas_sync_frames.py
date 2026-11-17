"""Per-frame pictures (SPEC 53.1): several frames on one canvas, one sync
engine. Each test drives two (or more) frame records on one manager through
a leg that used to be singular."""
import pytest
import rtoml

import fused_render.canvases as canvases_mod
from fused_render import canvas_snapshot
from test_canvas_sync_watcher import _quiet_watcher, _start, _state, _ws_client
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    _BASE_FILES,
    _cloned_shim_harness,
    _reset_module_state,
    fake_agent,
    harness,
)


@pytest.fixture
def m(harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    # a and b are nodes with rows, as on any canvas a frame shows.
    rows = [{"udfName": n, "x": 0, "y": 400 * i} for i, n in enumerate(["a", "b"])]
    (harness.clones / "alpha" / "canvas.toml").write_text(
        rtoml.dumps({"type": "canvas", "canvas": {"nodes": rows}}))
    manager = _start(harness)
    manager.shims = shims
    return manager


def _hello(m, frame, **kw):
    """A frame hydrates: a full cut, acked."""
    cut = m.cut_delta(True, frame=frame, **kw)
    assert cut["full"] is True, cut
    m.ack_delta(cut["rev"], True, frame=frame)
    return cut


def _body(names, x=None, rev=None, edges=()):
    body = {
        "dashboard_body": {"studioState": {"nodes": [
            {"id": f"n-{n}", "type": "simple-udf",
             "position": {"x": (x or {}).get(n, 0), "y": 400 * i},
             "width": 300, "height": 200, "data": {"udfName": n}}
            for i, n in enumerate(names)],
            "edges": [{"source": f"n-{s}", "target": f"n-{t}"} for s, t in edges]}},
        "udfs": [{"slug": n, "file": f"{n}.py"} for n in names],
        "body_channel": True,
    }
    if rev is not None:
        body["rev"] = rev
    return body


def _row(harness, name):
    doc = rtoml.loads((harness.clones / "alpha" / "canvas.toml").read_text())
    return next(r for r in doc["canvas"]["nodes"] if r["udfName"] == name)


def _op(cut, name):
    return next((o for o in cut["ops"] if o.get("name") == name), None)


def test_a_body_from_one_frame_is_a_delta_for_the_other_and_not_for_itself(m, harness):
    _hello(m, "A")
    _hello(m, "B")
    ack = m.apply_body("a", "a.py", canvas_snapshot.body_hash("a1\n"), "a2\n", frame="A")
    assert ack["ok"] and ack["changed"], ack
    assert (harness.clones / "alpha" / "a.py").read_text() == "a2\n"
    # A's own write never comes back to it.
    assert m.cut_delta(False, frame="A")["ops"] == []
    # B is owed it, once.
    cut = m.cut_delta(False, frame="B")
    op = _op(cut, "a")
    assert op is not None and op.get("code") == "a2\n", cut["ops"]
    m.ack_delta(cut["rev"], True, frame="B")
    assert m.cut_delta(False, frame="B")["ops"] == []


def test_a_cut_for_one_frame_does_not_bounce_the_other_frames_snapshot(m, harness):
    """`_cut_rev` was the manager's: a delta cut for B made A's next snapshot
    read as stale. Each frame is bounced only by a delta IT was sent."""
    a = _hello(m, "A")
    b = _hello(m, "B")
    first = m.apply_snapshot(1, 1, _body(["a", "b"], x={"a": 500}, rev=a["rev"]), frame="A")
    assert first["changed"] and not first.get("stale"), first
    assert _row(harness, "a")["x"] == 500
    owed = m.cut_delta(False, frame="B")           # B is sent A's move
    assert _op(owed, "a") is not None, owed["ops"]
    # A posts again, built on the rev it last saw: applied, not bounced.
    again = m.apply_snapshot(1, 2, _body(["a", "b"], x={"a": 600}, rev=a["rev"]), frame="A")
    assert again["changed"] and not again.get("rebuild"), again
    assert _row(harness, "a")["x"] == 600
    # B's snapshot from before it applied the delta IS bounced: nothing written.
    stale = m.apply_snapshot(7, 1, _body(["a", "b"], x={"b": 40}, rev=b["rev"]), frame="B")
    assert stale.get("rebuild") is True and stale["changed"] is False, stale
    assert _row(harness, "a")["x"] == 600


def test_a_frame_that_has_not_seen_a_new_node_does_not_delete_it(m, harness):
    a = _hello(m, "A")
    _hello(m, "B")
    clone = harness.clones / "alpha"
    # A adds node c (row first, then its body).
    m.apply_snapshot(1, 1, _body(["a", "b", "c"], rev=a["rev"]), frame="A")
    m.apply_body("c", "c.py", canvas_snapshot.body_hash(""), "c1\n", frame="A")
    assert (clone / "c.py").read_text() == "c1\n"
    # B, never told about c, posts its own full state (no rev: an older
    # workbench, so nothing bounces it).
    m.apply_snapshot(9, 1, _body(["a", "b"], x={"b": 70}), frame="B")
    assert (clone / "c.py").exists() and _row(harness, "c")["udfName"] == "c"
    assert _row(harness, "b")["x"] == 70
    # …and c is still owed to B.
    assert _op(m.cut_delta(False, frame="B"), "c") is not None


def test_generations_alternating_between_frames_reset_neither_view(m):
    a = _hello(m, "A")
    b = _hello(m, "B")
    for seq in (1, 2, 3):
        m.apply_snapshot(100, seq, _body(["a", "b"], x={"a": seq}, rev=m.disk_rev), frame="A")
        cut = m.cut_delta(False, frame="B")
        m.ack_delta(cut["rev"], True, frame="B")
        m.apply_snapshot(200, seq, _body(["a", "b"], x={"a": seq, "b": seq}, rev=cut["rev"]),
                         frame="B")
        cut = m.cut_delta(False, frame="A")
        assert cut["full"] is False, cut            # A's picture survived B's generation
        m.ack_delta(cut["rev"], True, frame="A")
    fa, fb = m._frames["A"], m._frames["B"]
    assert (fa.embed_gen, fb.embed_gen) == (100, 200)
    assert fa.embed_view is not None and fb.embed_view is not None
    assert a["rev"] <= fa.view_rev and b["rev"] <= fb.view_rev


def test_a_hard_pull_fences_every_frame_and_each_lifts_its_own(m, harness):
    _hello(m, "A")
    _hello(m, "B")
    res = harness.client.post("/api/canvases/sync/pull",
                              json={"env": ENV, "name": "alpha", "hard": True}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    fa, fb = m._frames["A"], m._frames["B"]
    assert fa.hard_fence and fb.hard_fence and fa.embed_view is None and fb.embed_view is None
    assert fa.body_fence_rev is not None and fb.body_fence_rev is not None
    # A applies the pulled state: its fences lift, B's hold.
    cut = m.cut_delta(False, frame="A")
    assert cut["full"] is True
    m.ack_delta(cut["rev"], True, frame="A")
    assert fa.hard_fence is None and fa.body_fence_rev is None
    assert fb.hard_fence is not None and fb.body_fence_rev is not None
    fenced = m.apply_body("a", "a.py", canvas_snapshot.body_hash("a1\n"), "old\n", frame="B")
    assert fenced == {"ok": False, "code": "fenced", "node": "a", "file": "a.py"}
    ok = m.apply_body("a", "a.py", canvas_snapshot.body_hash("a1\n"), "new\n", frame="A")
    assert ok["ok"] and ok["changed"], ok
    # A frame that arrives after the pull starts with no fence.
    late = m._frame("C")
    assert late.hard_fence is None and late.body_fence_rev is None


def test_sync_state_answers_for_the_frame_named_and_for_the_worst_off_otherwise(m, harness):
    _hello(m, "A")
    _hello(m, "B")
    (harness.clones / "alpha" / "a.py").write_text("moved\n")
    cut = m.cut_delta(False, frame="A")
    m.ack_delta(cut["rev"], True, frame="A")
    assert m.sync_state(frame="A")["legs"]["disk_to_embed"]["state"] == "idle"
    assert m.sync_state(frame="B")["legs"]["disk_to_embed"]["state"] == "pending"
    assert m.sync_state()["legs"]["disk_to_embed"]["state"] == "pending"
    cut = m.cut_delta(False, frame="B")
    m.ack_delta(cut["rev"], False, reason="boom", frame="B")
    assert m.sync_state(frame="A")["legs"]["disk_to_embed"]["state"] == "idle"
    assert m.sync_state()["legs"]["disk_to_embed"]["state"] == "error"
    # A frame id with no record answers as no frame named.
    assert m.sync_state(frame="nobody")["legs"]["disk_to_embed"]["state"] == "error"


def test_a_scoped_frame_reads_everything_and_writes_only_its_node(m, harness):
    q = "/api/canvases/delta?env=unstable&name=alpha&full=1&frame=N&frame_scope=a"
    cut = harness.client.get(q).json()
    assert cut["full"] is True and {o.get("name") for o in cut["ops"]} >= {"a", "b"}, cut
    assert m._frames["N"].scope == "a"
    post = {"env": ENV, "name": "alpha", "frame": "N"}
    res = harness.client.post("/api/canvases/snapshot", headers=GUARD,
                              json={**post, "gen": 1, "seq": 1, "body": _body(["a"])})
    assert res.status_code == 400 and res.json()["code"] == "scoped", res.text
    res = harness.client.post("/api/canvases/snapshot", headers=GUARD, json={
        **post, "gen": 1, "seq": 2, "rename": {"from": "a", "to": "z"}})
    assert res.status_code == 400 and res.json()["code"] == "scoped", res.text
    body = {**post, "base_hash": canvas_snapshot.body_hash("b1\n"), "text": "b2\n",
            "node": "b", "file": "b.py"}
    res = harness.client.post("/api/canvases/body", headers=GUARD, json=body)
    assert res.status_code == 400 and res.json()["code"] == "scoped", res.text
    assert (harness.clones / "alpha" / "b.py").read_text() == "b1\n"
    assert (harness.clones / "alpha" / "a.py").read_text() == "a1\n"
    body = {**post, "base_hash": canvas_snapshot.body_hash("a1\n"), "text": "a2\n",
            "node": "a", "file": "a.py"}
    res = harness.client.post("/api/canvases/body", headers=GUARD, json=body)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    # The scope is the record's for good: no later call widens or moves it.
    res = harness.client.get(q.replace("frame_scope=a", "frame_scope=b"))
    assert res.status_code == 400 and res.json()["code"] == "scoped", res.text
    assert m._frames["N"].scope == "a"


@pytest.mark.parametrize("query", [
    "frame=has%20space", "frame=" + "x" * 65, "frame=a/b",
    "frame_scope=a",                      # a scope needs a named frame
    "frame=N&frame_scope=..%2Fx",
])
def test_a_malformed_frame_is_a_400(m, harness, query):
    res = harness.client.get(f"/api/canvases/delta?env=unstable&name=alpha&{query}")
    assert res.status_code == 400, res.text
    assert set(m._frames) == {""}


def test_a_gone_frames_record_is_dropped_and_the_unnamed_one_never_is(m, monkeypatch):
    _hello(m, "A")
    _hello(m, "")
    monkeypatch.setattr(canvases_mod, "FRAME_DROP_S", 0.0)
    key = canvases_mod._sync_key(ENV, "alpha")
    canvases_mod._FRAME_SOCKETS[(key, "B")] = 1          # B's page is still open
    try:
        _hello(m, "B")
        m._frame("C")
        assert set(m._frames) == {"", "B", "C"}          # A went; "" and B stay
        # A comes back as a new record: its first cut is a full state.
        assert m.cut_delta(False, frame="A")["full"] is True
    finally:
        canvases_mod._FRAME_SOCKETS.pop((key, "B"), None)


def test_the_frame_limit_drops_an_idle_record_first_and_refuses_with_none(m, harness, monkeypatch):
    monkeypatch.setattr(canvases_mod, "MAX_FRAMES", 3)
    key = canvases_mod._sync_key(ENV, "alpha")
    m._frame("A")
    m._frame("B")
    m._frame("C")                                         # the oldest idle one (A) makes room
    assert set(m._frames) == {"", "B", "C"}
    canvases_mod._FRAME_SOCKETS[(key, "B")] = 1
    canvases_mod._FRAME_SOCKETS[(key, "C")] = 1
    try:
        res = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&frame=D")
        assert res.status_code == 409 and res.json()["code"] == "too_many_frames", res.text
        assert set(m._frames) == {"", "B", "C"}
    finally:
        canvases_mod._FRAME_SOCKETS.pop((key, "B"), None)
        canvases_mod._FRAME_SOCKETS.pop((key, "C"), None)


def test_the_events_socket_keeps_its_frames_record_and_sends_its_state(m, harness):
    client = _ws_client(harness)
    key = canvases_mod._sync_key(ENV, "alpha")
    url = "ws://127.0.0.1:1777/api/canvases/sync/events?env=unstable&name=alpha&frame="
    origin = {"origin": "http://127.0.0.1:1777"}
    with client.websocket_connect(url + "A", headers=origin) as ws:
        assert ws.receive_json()["type"] == "state"
        assert canvases_mod._FRAME_SOCKETS.get((key, "A")) == 1
    assert (key, "A") not in canvases_mod._FRAME_SOCKETS
    with pytest.raises(Exception):
        with client.websocket_connect(url + "bad%20id", headers=origin) as ws:
            ws.receive_json()


def test_a_page_that_names_no_frame_is_the_one_frame_it_always_was(m, harness):
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    res = harness.client.post("/api/canvases/delta/ack", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "rev": cut["rev"]})
    assert res.status_code == 200 and res.json()["ok"], res.text
    assert set(m._frames) == {""} and m._frames[""].seen is True
    assert m._view_rev == cut["rev"] and m._embed_view is m._frames[""].embed_view
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"


def test_a_skip_streak_is_one_frames_and_heals_only_that_frame(m):
    """#2311's skip streak (a frame that keeps declining deltas is asked for a
    full state) is per frame: another frame on the canvas is not held, and its
    page is not told to heal."""
    _hello(m, "A")
    _hello(m, "B")
    fa, fb = m._frame("A"), m._frame("B")
    for _ in range(canvases_mod.EMBED_SKIP_STREAK_ACKS):
        m._note_skip_streak(fa, {"a"})
    assert fa.skip_streak["stuck"] is True and fa.embed_heal_seq == 1
    assert fb.skip_streak is None and fb.embed_heal_seq == 0
    a, b = m.sync_state(frame="A"), m.sync_state(frame="B")
    assert a["legs"]["disk_to_embed"]["state"] == "held" and a["embed_heal_seq"] == 1
    assert b["legs"]["disk_to_embed"]["state"] != "held" and b["embed_heal_seq"] == 0
    assert b["embed_skipping"] is None
    # With no frame named, the stuck frame is the one reported.
    assert m.sync_state()["legs"]["disk_to_embed"]["state"] == "held"


def _two_frames_each_add_c(m, harness):
    """A and B each make a node named c (two windows, one next name); its file
    has not landed. Both records owe c's file."""
    a = _hello(m, "A")
    _hello(m, "B")
    m.apply_snapshot(1, 1, _body(["a", "b", "c"], rev=a["rev"]), frame="A")
    # B never saw A's c (no rev: an older workbench, nothing bounces it).
    m.apply_snapshot(9, 1, _body(["a", "b", "c"]), frame="B")
    assert m._frames["A"].fileless == {"c"} and m._frames["B"].fileless == {"c"}
    assert _row(harness, "c")["udfName"] == "c"


def test_a_dropped_frame_keeps_a_fileless_row_another_frame_still_owes(m, harness):
    _two_frames_each_add_c(m, harness)
    m._drop_frame("A")
    # B still owes c's file: its row stays.
    assert _row(harness, "c")["udfName"] == "c"
    # Once B lets go too, the row goes, as before.
    m._drop_frame("B")
    doc = rtoml.loads((harness.clones / "alpha" / "canvas.toml").read_text())
    assert "c" not in [r["udfName"] for r in doc["canvas"]["nodes"]]


def test_a_new_generation_keeps_a_fileless_row_another_frame_still_owes(m, harness):
    _two_frames_each_add_c(m, harness)
    # A re-hydrates (a new generation) and posts without c.
    m.apply_snapshot(2, 1, _body(["a", "b"]), frame="A")
    assert m._frames["A"].fileless == set()
    assert _row(harness, "c")["udfName"] == "c"


def _edges(harness):
    doc = rtoml.loads((harness.clones / "alpha" / "canvas.toml").read_text())
    return [list(e) for e in doc["canvas"].get("edges") or []]


def test_a_frame_that_has_not_seen_another_frames_edge_is_bounced_not_applied(m, harness):
    """SPEC 53.1 FP-6: A adds an edge; B, built on a rev from before it (no
    delta with it cut for B yet), posts its full state. Applied, B's edge
    set (without a->b) would replace A's. It is bounced instead, and B is
    owed the delta that brings the edge."""
    a = _hello(m, "A")
    b = _hello(m, "B")
    res = m.apply_snapshot(1, 1, _body(["a", "b"], edges=[("a", "b")], rev=a["rev"]), frame="A")
    assert res["changed"], res
    assert _edges(harness) == [["a", "b"]]
    stale = m.apply_snapshot(9, 1, _body(["a", "b"], x={"b": 70}, rev=b["rev"]), frame="B")
    assert stale.get("rebuild") is True and stale["changed"] is False, stale
    assert _edges(harness) == [["a", "b"]]
    assert _row(harness, "b")["x"] == 0
    # B is owed the edge; once it applied that delta its re-post lands.
    cut = m.cut_delta(False, frame="B")
    assert cut["rev"] >= stale["rev"], (cut, stale)
    edge_op = next(o for o in cut["ops"] if o.get("op") == "edges")
    assert edge_op["set"] == [["a", "b"]]
    m.ack_delta(cut["rev"], True, frame="B")
    again = m.apply_snapshot(9, 2, _body(["a", "b"], x={"b": 70}, edges=[("a", "b")],
                                         rev=cut["rev"]), frame="B")
    assert again["changed"] and not again.get("rebuild"), again
    assert _edges(harness) == [["a", "b"]] and _row(harness, "b")["x"] == 70


def test_a_frame_with_no_rev_is_not_bounced_by_another_frames_write(m, harness):
    """An older workbench sends no `body.rev`: applied as today."""
    a = _hello(m, "A")
    _hello(m, "B")
    m.apply_snapshot(1, 1, _body(["a", "b"], x={"a": 5}, rev=a["rev"]), frame="A")
    res = m.apply_snapshot(9, 1, _body(["a", "b"], x={"b": 70}), frame="B")
    assert res["changed"] and not res.get("rebuild"), res
    assert _row(harness, "b")["x"] == 70


def test_a_frames_own_write_never_bounces_its_next_snapshot(m, harness):
    a = _hello(m, "A")
    _hello(m, "B")
    m.apply_snapshot(1, 1, _body(["a", "b"], edges=[("a", "b")], rev=a["rev"]), frame="A")
    again = m.apply_snapshot(1, 2, _body(["a", "b"], x={"a": 9}, rev=a["rev"]), frame="A")
    assert again["changed"] and not again.get("rebuild"), again
    assert _row(harness, "a")["x"] == 9


def test_another_frames_write_forces_one_recut_however_many_stale_posts(m, harness):
    a = _hello(m, "A")
    b = _hello(m, "B")
    m.apply_snapshot(1, 1, _body(["a", "b"], edges=[("a", "b")], rev=a["rev"]), frame="A")
    first = m.apply_snapshot(9, 1, _body(["a", "b"], x={"b": 1}, rev=b["rev"]), frame="B")
    rev_after_first = m.disk_rev
    second = m.apply_snapshot(9, 2, _body(["a", "b"], x={"b": 2}, rev=b["rev"]), frame="B")
    assert first.get("rebuild") and second.get("rebuild"), (first, second)
    assert first["rev"] == second["rev"] == rev_after_first
    assert m.disk_rev == rev_after_first           # the second forced nothing
    assert _edges(harness) == [["a", "b"]] and _row(harness, "b")["x"] == 0
