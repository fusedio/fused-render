"""experts/capture.py and experts/scheduler.py (docs/experts.md §4, §7).

The Tasks listing, the agent's history read, the canvas lookup, the consult
log and `model.text` are all faked: no transcript parser, no model, no
bots registry. Observations and memory are the real modules on a stand-in
bot (`id`, `dir`, `meta`).
"""
import json
import threading
import types

import pytest

from fused_render.experts import capture, consult, memory, model, observations, scheduler, store

NOW = 1_800_000_000.0


class _Bot:
    def __init__(self, tmp_path, bid="e1", targets=("canvas",), learns=None):
        self.id = bid
        self.dir = str(tmp_path / bid / ".fused")
        man = {"ask_me_about": "Writing canvases", "offered_to": {"targets": list(targets)}}
        if learns is not None:
            man["learns_from"] = learns
        self.meta = {"id": bid, "name": "Canvas Expert", "kind": "expert", "expert": man}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake Tasks listing of transcripts on disk, and every collaborator."""
    state = {"tasks": {}, "history_calls": [], "prompts": [], "answers": [], "log": []}
    monkeypatch.setattr(capture, "_watermark_path", lambda: str(tmp_path / "experts-captured.json"))

    def add(sid, *, age, size=100, canvas=False, backend="claude"):
        path = tmp_path / f"{sid}.jsonl"
        path.write_bytes(b"x" * size)
        t = NOW - age
        import os
        os.utime(path, (t, t))
        target = str(tmp_path / ("clone" if canvas else "proj"))
        state["tasks"][sid] = {"key": sid, "session_id": sid, "path": str(path), "backend": backend,
                               "entries": [], "_target": target}
        return path

    def place(task):
        task["target"] = task["_target"]

    tasks = types.SimpleNamespace(
        _collect=lambda: {k: dict(v) for k, v in state["tasks"].items()}, _place=place,
        _export_markdown=lambda task, turns, backend: "# T\n" + "\n".join(t["text"] for t in turns))
    monkeypatch.setattr(capture, "_tasks", lambda: tasks)

    def history(file, session_id, transcript=""):
        state["history_calls"].append((file, session_id, transcript))
        return {"turns": [{"role": "assistant", "text": "early " + "y" * 50000},
                          {"role": "user", "text": "please always use fused.load"}], "backend": "claude"}

    monkeypatch.setattr(capture, "_agent", lambda: types.SimpleNamespace(_history=history))
    monkeypatch.setattr(capture, "_canvas_clone", lambda path: path if path.endswith("clone") else "")
    monkeypatch.setattr(capture, "_last_active", lambda path, mtime: mtime)

    def text(bot, prompt, **kw):
        state["prompts"].append(prompt)
        return state["answers"].pop(0) if state["answers"] else "[]"

    monkeypatch.setattr(model, "text", text)
    monkeypatch.setattr(model, "available", lambda bot: (True, ""))
    monkeypatch.setattr(consult, "log", lambda bot, limit=100: list(state["log"]))
    monkeypatch.setattr(capture.time, "time", lambda: NOW)
    state["add"] = add
    return state


def test_due_sessions_idle_lookback_and_watermark(env, tmp_path):
    env["add"]("busy", age=60)
    env["add"]("idle", age=3600, canvas=True)
    env["add"]("ancient", age=30 * 86400)
    due = capture.due_sessions(NOW)
    assert [s["session_id"] for s in due] == ["idle"]
    s = due[0]
    assert s["canvas"].endswith("clone") and s["backend"] == "claude" and s["size"] == 100
    assert s["last_active"] == NOW - 3600 and s["target"].endswith("clone")
    capture.mark_captured(s)
    assert capture.due_sessions(NOW) == []
    marks = json.load(open(tmp_path / "experts-captured.json"))
    assert marks["idle"]["size"] == 100 and marks["idle"]["at"] == NOW
    env["add"]("idle", age=3600, size=150, canvas=True)  # the session was resumed and grew
    assert [s["session_id"] for s in capture.due_sessions(NOW)] == ["idle"]


def test_relevance_by_offered_to_and_by_consult_log(env, tmp_path):
    canvas_bot = _Bot(tmp_path, "c", targets=["canvas"])
    everyone = _Bot(tmp_path, "a", targets=["*"])
    nobody = _Bot(tmp_path, "n", targets=[])
    plain = {"session_id": "s1", "canvas": ""}
    in_canvas = {"session_id": "s2", "canvas": "/clone"}
    assert capture.relevant(everyone, plain) and capture.relevant(everyone, in_canvas)
    assert capture.relevant(canvas_bot, in_canvas) and not capture.relevant(canvas_bot, plain)
    assert not capture.relevant(nobody, plain)
    env["log"] = [{"id": "c1", "question": "q", "caller": {"session_id": "s1", "run_id": "r1"}}]
    assert capture.relevant(nobody, plain) and not capture.relevant(nobody, in_canvas)


def test_relevance_by_run_id_when_the_session_was_not_known_yet(env, tmp_path, monkeypatch):
    from fused_render.experts import routes
    monkeypatch.setattr(routes, "caller_for_run", lambda rid: {"session_id": "s9" if rid == "r9" else ""})
    env["log"] = [{"id": "c1", "caller": {"session_id": "", "run_id": "r9"}}]
    assert capture.relevant(_Bot(tmp_path, "n", targets=[]), {"session_id": "s9", "canvas": ""})


def test_capture_session_prompt_parse_cap_and_trust(env, tmp_path):
    bot = _Bot(tmp_path)
    memory.add(bot, "Prefer fused.load over HTTP calls.", trust="user")
    env["log"] = [{"id": "c1", "question": "How to chain?", "answer": "Use fused.load.", "unsure": True,
                   "caller": {"session_id": "idle"}},
                  {"id": "c0", "question": "Other session", "answer": "x", "caller": {"session_id": "zzz"}}]
    env["add"]("idle", age=3600, canvas=True)
    session = capture.due_sessions(NOW)[0]
    rows = [{"kind": "user_correction", "claim": f"Claim {i}", "about": "fused.load"} for i in range(8)]
    rows.insert(0, {"kind": "made_up", "claim": "dropped"})
    rows.insert(1, {"kind": "error then fix", "claim": "  Error   then fix works. "})
    env["answers"] = ["Here you go:\n```json\n" + json.dumps(rows) + "\n```"]
    obs = capture.capture_session(bot, session)
    assert len(obs) == capture.MAX_OBS == 5
    assert [o["kind"] for o in obs[:2]] == ["error_then_fix", "user_correction"]
    assert obs[0]["claim"] == "Error then fix works."
    assert obs[1]["about"] == ["fused.load"]
    assert all(o["trust"] == "outcome" and o["session"] == "idle" for o in obs)
    assert len(observations.pending(bot)) == 5
    assert memory.items(bot)[0]["count"] == 1  # capture never writes memory

    prompt = env["prompts"][-1]
    for must in ("a pattern or convention the USER asked for", "user corrected", "fix demonstrably worked",
                 "confirmed or contradicted", "merely read", "NOT a lesson", "NEW relative to",
                 "Zero is a normal", '"kind"', '"claim"', '"about"', "Writing canvases",
                 "- Prefer fused.load over HTTP calls.", "Q: How to chain?", "A (unsure): Use fused.load.",
                 "please always use fused.load"):
        assert must in prompt, must
    assert "Other session" not in prompt
    # The transcript is the tail, capped.
    assert "earlier conversation cut" in prompt
    assert len(prompt) < capture.TRANSCRIPT_CAP + 6000
    assert env["history_calls"][-1][1:] == ("idle", session["path"])
    # Watermarked: a second pass finds nothing until the transcript grows.
    assert capture.due_sessions(NOW) == []


@pytest.mark.parametrize("answer,expected", [
    ("[]", []),
    ("no lessons here", []),
    ('{"observations": [{"kind": "repeated_request", "claim": "A", "about": ["x", ""]}]}',
     [{"kind": "repeated_request", "claim": "A", "about": ["x"]}]),
    ('{"kind": "answer_contradicted", "claim": "B"}', [{"kind": "answer_contradicted", "claim": "B", "about": []}]),
    ('noise [not json] then [{"kind": "user-correction", "claim": "C"}]',
     [{"kind": "user_correction", "claim": "C", "about": []}]),
    ('[{"kind": "unanswered", "claim": "gap"}, {"kind": "user_correction", "claim": ""}, 3]', []),
])
def test_lenient_parse(answer, expected):
    assert capture.parse(answer) == expected


def test_kinds_follow_learns_from(env, tmp_path):
    bot = _Bot(tmp_path, learns=["user_correction"])
    env["add"]("idle", age=3600, canvas=True)
    session = capture.due_sessions(NOW)[0]
    env["answers"] = [json.dumps([{"kind": "error_then_fix", "claim": "x"},
                                  {"kind": "user_correction", "claim": "y"}])]
    assert [o["claim"] for o in capture.capture_session(bot, session)] == ["y"]


def test_a_model_failure_writes_nothing_and_retries_later(env, tmp_path, monkeypatch):
    bot = _Bot(tmp_path)
    env["add"]("idle", age=3600, canvas=True)
    session = capture.due_sessions(NOW)[0]

    def boom(*a, **k):
        raise model.Unavailable("off")

    monkeypatch.setattr(model, "text", boom)
    with pytest.raises(model.Unavailable):
        capture.capture_session(bot, session)
    assert observations.pending(bot) == [] and capture.due_sessions(NOW)


def test_capture_due_runs_relevant_experts_and_marks(env, tmp_path, monkeypatch):
    canvas_bot = _Bot(tmp_path, "c", targets=["canvas"])
    other = _Bot(tmp_path, "o", targets=[])
    monkeypatch.setattr(store, "all_experts", lambda: [canvas_bot, other])
    env["add"]("in-canvas", age=3600, canvas=True)
    env["add"]("plain", age=4000)
    env["answers"] = [json.dumps([{"kind": "user_correction", "claim": "Use X."}])]
    res = capture.capture_due(NOW)
    assert res == {"sessions": 2, "observations": 1, "errors": 0}
    assert len(env["prompts"]) == 1  # only the canvas expert, only on the canvas session
    assert [o["session"] for o in observations.pending(canvas_bot)] == ["in-canvas"]
    assert observations.pending(other) == []
    assert capture.due_sessions(NOW) == []  # both marked, the unwanted one too
    assert capture.capture_due(NOW)["sessions"] == 0


def test_capture_due_without_available_experts_reads_nothing(env, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "all_experts", lambda: [_Bot(tmp_path)])
    monkeypatch.setattr(model, "available", lambda bot: (False, "off"))
    env["add"]("idle", age=3600, canvas=True)
    assert capture.capture_due(NOW) == {"sessions": 0, "observations": 0, "errors": 0}
    assert capture.due_sessions(NOW)  # not marked


# ------------------------------------------------------------------ scheduler ---
@pytest.fixture
def sched(monkeypatch, tmp_path):
    scheduler.reset()
    order = []
    bots = [_Bot(tmp_path, "a"), _Bot(tmp_path, "b")]
    mods = {
        "store": types.SimpleNamespace(all_experts=lambda: bots),
        "capture": types.SimpleNamespace(capture_due=lambda limit, stop=None: order.append(("capture", limit))
                                         or {"sessions": 1}),
        "model": types.SimpleNamespace(available=lambda bot: (bot.id != "off", "")),
        "observations": types.SimpleNamespace(due=lambda bot: order.append(("due", bot.id)) or bot.id == "a"),
        "consolidate": types.SimpleNamespace(run=lambda bot: order.append(("consolidate", bot.id))
                                             or {"ran": True}),
        "routes": types.SimpleNamespace(propose=lambda bot: order.append(("propose", bot.id))
                                        or (["p1"] if bot.id == "a" else [])),
        "cards": types.SimpleNamespace(tick=lambda: order.append(("cards",))),
    }
    monkeypatch.setattr(scheduler, "_mod", lambda name: mods[name])
    monkeypatch.setattr(scheduler, "_owner", lambda: True)
    lease = {"held": False, "free": True, "released": 0}

    def acquire():
        lease["held"] = lease["free"]
        return lease["free"]

    def release():
        lease["released"] += 1
        lease["held"] = False

    monkeypatch.setattr(scheduler, "_acquire", acquire)
    monkeypatch.setattr(scheduler, "_release", release)
    yield types.SimpleNamespace(order=order, bots=bots, mods=mods, lease=lease)
    scheduler.reset()


def test_tick_order(sched):
    out = scheduler.tick()
    assert out["ran"] and out["errors"] == 0
    assert sched.order == [("capture", scheduler.CAPTURE_PER_TICK),
                           ("due", "a"), ("consolidate", "a"), ("propose", "a"),
                           ("due", "b"), ("propose", "b"), ("cards",)]
    assert out["consolidated"] == ["a"] and out["proposed"] == ["p1"]
    assert sched.lease["released"] == 1 and not sched.lease["held"]


def test_tick_never_raises_and_carries_on(sched):
    def boom(*a, **k):
        raise RuntimeError("x")

    sched.mods["capture"].capture_due = boom
    sched.mods["consolidate"].run = boom
    out = scheduler.tick()
    assert out["ran"] and out["errors"] == 2
    assert ("cards",) in sched.order and ("propose", "a") in sched.order
    sched.mods["store"].all_experts = boom
    sched.mods["cards"].tick = boom
    out = scheduler.tick()
    assert out["errors"] >= 3
    assert sched.lease["released"] == 2


def test_tick_skips_without_the_lease_or_the_bots(sched, monkeypatch):
    sched.lease["free"] = False
    out = scheduler.tick()
    assert not out["ran"] and "lease" in out["skipped"] and sched.order == []
    assert sched.lease["released"] == 0
    sched.lease["free"] = True
    monkeypatch.setattr(scheduler, "_owner", lambda: False)
    out = scheduler.tick()
    assert not out["ran"] and "own" in out["skipped"] and sched.order == []


def test_unavailable_experts_are_skipped(sched):
    sched.bots[1].id = "off"
    scheduler.tick()
    assert not [o for o in sched.order if len(o) > 1 and o[1] == "off"]


def test_start_is_idempotent_and_shutdown_is_clean(sched, monkeypatch):
    monkeypatch.setattr(scheduler, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(scheduler, "TICK_S", 0.05)
    ticked = threading.Event()
    sched.mods["cards"].tick = lambda: ticked.set()
    before = threading.active_count()
    scheduler.start()
    scheduler.start()
    assert scheduler.started()
    assert threading.active_count() == before + 1
    assert ticked.wait(5)
    thread = scheduler._thread
    scheduler.shutdown()
    assert not scheduler.started() and not thread.is_alive()
    scheduler.shutdown()  # a second shutdown is a no-op
    scheduler.start()     # and it can start again
    assert scheduler.started()


def test_the_real_lease_is_exclusive(tmp_path, monkeypatch):
    from fused_render import _platform, tasks_store
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    tasks_store.reset_leases_for_tests()
    try:
        assert scheduler._acquire()
        # Another open file description (another process, in effect) is refused.
        other = tasks_store._open_lease_file(scheduler.LEASE, machine_wide=True)
        assert not _platform.flock(other, blocking=False)
        scheduler._release()
        assert _platform.flock(other, blocking=False)
        other.close()
    finally:
        tasks_store.reset_leases_for_tests()
