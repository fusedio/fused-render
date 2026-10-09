"""The events bus (fused_render/server/events.py) and its `/api/events` route:
the invariants the Fused Events Bus design names, each pinned once.

  * a subscribe always answers with a snapshot before any delta
  * snapshot body == GET body for the same params (per topic)
  * at most one unsent frame per subscription (latest wins)
  * a poll-and-diff producer lives only while its key has a subscriber
  * publish is safe from a worker thread
  * every socket that reaches app state checks Origin
  * LanApp closes every socket, the events route included
  * hidden policy is the client's: the catalog says which topics may be dropped
  * no topic name appears outside runtime.js and frontend/src (D15: no page API)
  * every poll-and-diff producer names the client cadence it replaces (D16)
"""
import asyncio
import json
import os
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs as jobs_mod
from fused_render import tasks_watch
from fused_render.server import create_app
from fused_render.server.events import EventBus, Topic, bus, simple_topic

# The test client stamps `Host: testserver` whatever the base URL; `ws_origin_ok`
# wants a loopback Host that the Origin names, so both are set explicitly.
ORIGIN = {"origin": "http://127.0.0.1", "host": "127.0.0.1"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    jobs_mod.reset()
    bus.reset()
    yield h
    bus.reset()
    jobs_mod.reset()


def _client(tmp_path, **kw):
    return TestClient(create_app(start_dir=str(tmp_path)), base_url="http://127.0.0.1",
                      headers=ORIGIN, **kw)


def _recv(ws, want=None, tries=50):
    """The next frame, or the next frame of kind `want` (pings skipped)."""
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("t") == "ping":
            continue
        if want is None or msg.get("t") == want:
            return msg
    raise AssertionError(f"no {want} frame arrived")


# ------------------------------------------------------------------ the route

def test_hello_then_snapshot_before_anything_else(home, tmp_path):
    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"
        assert hello["boot_id"] and hello["pid"] == os.getpid()
        # The catalog carries the hidden policy for the client (D7).
        assert hello["topics"]["jobs"]["hidden_ok"] is False
        assert hello["topics"]["tasks.listing"]["hidden_ok"] is True
        ws.send_json({"t": "sub", "id": 1, "topic": "jobs", "params": {}})
        snap = _recv(ws, "snap")
        assert snap["id"] == 1
        assert snap["body"]["jobs"] == [] and "now" in snap["body"]


def test_snapshot_equals_the_get_body(home, tmp_path):
    client = _client(tmp_path)
    client.post("/api/jobs", json={"id": "dl-1", "title": "A file", "state": "running"},
                headers={"X-Fused": "1"})
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": "j", "topic": "jobs", "params": {}})
        snap = _recv(ws, "snap")["body"]
    via_get = client.get("/api/jobs").json()
    assert [j["id"] for j in snap["jobs"]] == [j["id"] for j in via_get["jobs"]] == ["dl-1"]
    # `{id}` narrows to one row, the way `fused.watchJob` wants it.
    client.post("/api/jobs", json={"id": "dl-2", "title": "B", "state": "running"},
                headers={"X-Fused": "1"})
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 2, "topic": "jobs", "params": {"id": "dl-2"}})
        assert [j["id"] for j in _recv(ws, "snap")["body"]["jobs"]] == ["dl-2"]


def test_a_write_pushes_a_fresh_snapshot(home, tmp_path):
    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 1, "topic": "jobs", "params": {}})
        assert _recv(ws, "snap")["body"]["jobs"] == []
        client.post("/api/jobs", json={"id": "dl-1", "title": "A file", "state": "running"},
                    headers={"X-Fused": "1"})
        pushed = _recv(ws, "snap")
        assert [j["id"] for j in pushed["body"]["jobs"]] == ["dl-1"]
        client.post("/api/jobs/dl-1/cancel", headers={"X-Fused": "1"})
        pushed = _recv(ws, "snap")
        assert pushed["body"]["jobs"][0]["cancel_requested"] is True


def test_unknown_topic_and_bad_params_answer_err(home, tmp_path):
    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 1, "topic": "nope", "params": {}})
        err = _recv(ws, "err")
        assert err["id"] == 1 and err["status"] == 404
        ws.send_json({"t": "sub", "id": 2, "topic": "fs.watch", "params": {"paths": ["relative"]}})
        err = _recv(ws, "err")
        assert err["id"] == 2 and err["status"] == 400 and "absolute" in err["error"]
        # The GET's own refusal, same body (bad scope → 400).
        ws.send_json({"t": "sub", "id": 3, "topic": "tasks.listing", "params": {"scope": "nope"}})
        err = _recv(ws, "err")
        assert err["id"] == 3 and err["status"] == 400


def test_origin_is_checked(home, tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)), base_url="http://127.0.0.1")
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/events", headers={"origin": "http://evil.example", "host": "127.0.0.1"}):
            pass
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/events"):
            pass


def test_every_websocket_route_checks_origin_or_is_allowlisted():
    """Enumerate `@router.websocket` handlers and assert each references
    `ws_origin_ok` — the invariant D10 logs so it is not undone. The capture
    stream authenticates with its own token and is the one allowlisted."""
    import inspect
    from fused_render.server.routers import capture, events, run, terminal
    allow = {capture.api_capture_stream.__name__}
    seen = 0
    for mod in (events, run, terminal, capture):
        for name, fn in inspect.getmembers(mod, inspect.iscoroutinefunction):
            src = inspect.getsource(fn)
            if "@router.websocket" not in src:
                continue  # a helper a route hands its socket to, not a route
            seen += 1
            if name in allow:
                continue
            assert "ws_origin_ok" in src, f"{mod.__name__}.{name} does not check Origin"
    assert seen >= 4


def test_lan_app_closes_the_events_socket(home, tmp_path, monkeypatch):
    from fused_render.lan import LanApp
    inner = create_app(start_dir=str(tmp_path))
    wrapped = TestClient(LanApp(inner), base_url="http://127.0.0.1", headers=ORIGIN)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with wrapped.websocket_connect("/api/events"):
            pass
    assert exc.value.code == 1008


# ---------------------------------------------------------------- fs.watch

def test_fs_watch_snapshot_then_a_pushed_delta(home, tmp_path, monkeypatch):
    from fused_render.server import watch as _server_watch
    monkeypatch.setattr(_server_watch, "_LOCAL_POLL_S", 0.02)
    watched = tmp_path / "w.html"
    watched.write_text("a", encoding="utf-8")
    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 1, "topic": "fs.watch", "params": {"paths": [str(watched)]}})
        snap = _recv(ws, "snap")
        assert snap["body"]["paths"][0]["path"] == str(watched)
        assert bus.subscriber_count("fs.watch") == 1
        # The registry ticker is alive for the one path, refcounted by the bus.
        time.sleep(0.1)
        os.utime(watched, (time.time() + 5, time.time() + 5))
        delta = _recv(ws, "delta")
        assert delta["id"] == 1
        assert delta["body"]["changes"][0]["path"] == str(watched)
        ws.send_json({"t": "unsub", "id": 1})
        time.sleep(0.05)
        assert bus.subscriber_count("fs.watch") == 0
    assert str(watched) not in _server_watch._WATCH_REGISTRY._entries


# ------------------------------------------------------------- tasks.listing

def test_tasks_listing_snapshot_matches_get_and_delta_follows_a_bump(home, tmp_path):
    client = _client(tmp_path)
    via_get = client.get("/api/tasks").json()
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 1, "topic": "tasks.listing", "params": {}})
        snap = _recv(ws, "snap")
        assert snap["body"]["tasks"] == via_get["tasks"]
        assert snap["gen"] == snap["body"]["generation"]
        # A named change the ring can answer: a delta, not a snapshot. The key
        # names no listed row, so it comes back as `gone` — which is the shape
        # the client folds (`mergeTaskChanges`), and proves the cursor moved
        # server-side without the client re-asking.
        tasks_watch.notify({"nobody-such-key"})
        delta = _recv(ws, "delta")
        assert delta["id"] == 1
        assert delta["gen"] > snap["gen"]
        assert "nobody-such-key" in delta["body"]["gone"]
        assert "drafts" in delta["body"]


def test_tasks_listing_resubscribe_answers_a_snapshot_then_deltas(home, tmp_path):
    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 1, "topic": "tasks.listing", "params": {}})
        gen = _recv(ws, "snap")["gen"]
    tasks_watch.notify({"k1"})
    tasks_watch.notify({"k2"})
    with client.websocket_connect("/api/events") as ws:
        ws.receive_json()
        ws.send_json({"t": "sub", "id": 7, "topic": "tasks.listing", "params": {}, "since": gen})
        # A snapshot first, always (D4), whatever `since` says: the opening
        # frame is the GET's body, standing for the newer generation.
        snap = _recv(ws, "snap")
        assert snap["gen"] >= gen + 2
        # ...and a bump after it is a delta from THAT generation.
        tasks_watch.notify({"k3"})
        delta = _recv(ws, "delta")
        assert delta["gen"] > snap["gen"] and "k3" in delta["body"]["gone"]


def test_tasks_listing_a_window_past_the_ring_degrades_to_a_snapshot(home, tmp_path, monkeypatch):
    """The topic's own answer for a client further behind than the ring
    remembers is None — which the bus turns into a snapshot (D4)."""
    from fused_render.server.topics import TasksListingTopic
    tasks_watch.reset()
    _client(tmp_path)  # builds the app: the topic reads the router's listing
    topic = TasksListingTopic()
    params = topic.validate({})
    start = tasks_watch.generation()
    tasks_watch.notify({"k1"})
    answer = topic.delta(params, start)
    assert answer is not None and answer is not topic.delta  # a delta: (body, gen)
    body, gen = answer
    assert gen == start + 1 and "k1" in body["gone"]
    for i in range(tasks_watch.RING + 5):
        tasks_watch.notify({f"k{i}"})
    assert topic.delta(params, start) is None, "a window past the ring must be a snapshot"
    # Nothing moved since the client's generation: no frame at all.
    now = tasks_watch.generation()
    body, gen = topic.delta(params, now)
    assert gen == now and body["rows"] == [] and body["gone"] == []


# --------------------------------------------------------------- the core bus

class _Counting(Topic):
    name = "t.counting"
    kind = "polldiff"
    poll_interval_s = 0.01
    signature_is_snapshot = True

    def __init__(self):
        self.reads = 0
        self.value = 0

    def snapshot(self, params):
        self.reads += 1
        return {"value": self.value, "p": params.get("p")}

    def signature(self, params):
        return self.snapshot(params)


class _Sink:
    def __init__(self):
        self.frames: list = []
        self.event = asyncio.Event()

    async def send(self, text: str):
        self.frames.append(json.loads(text))
        self.event.set()


async def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


def test_polldiff_producer_lives_only_while_subscribed():
    async def scenario():
        b = EventBus()
        topic = _Counting()
        b.register(topic)
        b.bind()
        sink = _Sink()
        conn = b.connect(sink.send)
        sub = await b.subscribe(conn, 1, "t.counting", {"p": 1})
        await _wait_for(lambda: any(f.get("t") == "snap" for f in sink.frames))
        assert b.producer_tasks() == 1
        topic.value = 5
        await _wait_for(lambda: sum(1 for f in sink.frames if f.get("t") == "snap") >= 2)
        assert sink.frames[-1]["body"]["value"] == 5
        b.unsubscribe(sub)
        await asyncio.sleep(0.02)
        assert b.producer_tasks() == 0, "a producer outlived its last subscriber"
        # A second subscriber to the same key shares one producer and one build.
        s1 = await b.subscribe(conn, 2, "t.counting", {"p": 2})
        s2 = await b.subscribe(conn, 3, "t.counting", {"p": 2})
        await _wait_for(lambda: sum(1 for f in sink.frames if f.get("id") in (2, 3) and f["t"] == "snap") == 2)
        assert b.producer_tasks() == 1
        b.unsubscribe(s1)
        assert b.producer_tasks() == 1
        b.unsubscribe(s2)
        await asyncio.sleep(0.02)
        assert b.producer_tasks() == 0
        b.disconnect(conn)
    asyncio.run(scenario())


def test_at_most_one_unsent_frame_per_subscription_latest_wins():
    async def scenario():
        b = EventBus()
        topic = simple_topic("t.v", lambda p: {"n": state["n"]}, kind="write")
        state = {"n": 0}
        b.register(topic)
        b.bind()
        sent: list = []
        gate = asyncio.Event()

        async def slow_send(text):
            await gate.wait()  # the client is not reading
            sent.append(json.loads(text))

        conn = b.connect(slow_send)
        sub = await b.subscribe(conn, 1, "t.v", {})
        await asyncio.sleep(0.05)
        # Twenty publishes while the socket is wedged: the pending dict holds
        # ONE frame for the subscription, the newest.
        for i in range(1, 21):
            state["n"] = i
            b.publish("t.v")
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.05)
        assert len(conn.pending) <= 1
        gate.set()
        await _wait_for(lambda: len(sent) >= 2)
        await asyncio.sleep(0.05)
        values = [f["body"]["n"] for f in sent if f.get("t") == "snap"]
        assert values[0] == 0
        assert values[-1] == 20
        assert len(values) < 21, "every publish was sent, nothing coalesced"
        b.unsubscribe(sub)
        b.disconnect(conn)
    asyncio.run(scenario())


def test_publish_from_a_worker_thread():
    async def scenario():
        b = EventBus()
        state = {"n": 0}
        b.register(simple_topic("t.thread", lambda p: {"n": state["n"]}, kind="write"))
        b.bind()
        sink = _Sink()
        conn = b.connect(sink.send)
        await b.subscribe(conn, 1, "t.thread", {})
        await _wait_for(lambda: len(sink.frames) == 1)
        state["n"] = 1

        def worker():
            b.publish("t.thread")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        await _wait_for(lambda: len(sink.frames) == 2)
        assert sink.frames[-1]["body"]["n"] == 1
        b.disconnect(conn)
    asyncio.run(scenario())


def test_ping_when_silent():
    async def scenario():
        from fused_render.server import events as ev
        orig = ev.PING_EVERY_S
        ev.PING_EVERY_S = 0.05
        try:
            b = EventBus()
            b.bind()
            sink = _Sink()
            conn = b.connect(sink.send)
            await _wait_for(lambda: any(f.get("t") == "ping" for f in sink.frames), timeout=1.0)
            b.disconnect(conn)
        finally:
            ev.PING_EVERY_S = orig
    asyncio.run(scenario())


# ----------------------------------------------------------- D15 / D16 guards

def _topic_names() -> list[str]:
    from fused_render.server import topics as topics_mod
    topics_mod.register_all()
    return sorted(bus.topics)


def test_no_topic_name_appears_in_templates_or_skills():
    """D15: topic names are internal plumbing. A template or a skill that
    names one would be a page API by the back door."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    # Dotted names only: a bare word like `jobs` is also a JSON key every
    # job-reporting template reads (`data["jobs"]`), and that is the GET's
    # body, not a topic. Every topic but `jobs` and `bots` is dotted.
    names = [n for n in _topic_names() if "." in n]
    assert names, "no topics registered"
    hits = []
    for base in (root / "fused_render" / "templates", root / "skills", root / ".claude" / "skills"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".js", ".html", ".md", ".ts", ".tsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name in names:
                if f'"{name}"' in text or f"'{name}'" in text:
                    hits.append(f"{path.relative_to(root)}: {name}")
    assert not hits, "topic names leaked outside runtime.js/frontend:\n" + "\n".join(hits)


def test_every_poll_and_diff_producer_names_the_cadence_it_replaces():
    """D16: a server-side producer runs at the OLD client interval, and the
    source says which client file:line that interval came from."""
    import inspect
    from fused_render.server import topics as topics_mod
    topics_mod.register_all()
    for name, topic in bus.topics.items():
        if not topic.poll_interval_s:
            continue
        src = inspect.getsource(type(topic)) if type(topic) is not type(bus.topics["tasks.listing"]).__base__ else ""
        if not src:
            src = topic.__dict__.get("cadence_note", "")
        assert "old" in src.lower() and ("cadence" in src.lower() or "interval" in src.lower()), \
            f"{name}: poll_interval_s={topic.poll_interval_s} is not annotated with the client cadence it replaces"
