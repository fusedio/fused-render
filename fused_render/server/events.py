"""The Fused Events Bus: every live fact in a browser is a subscription.

WHY (measured 2026-10-08, decided 2026-10-09). Every native WKWebView window
of the app shares one data store, so all of them share WebKit's pool of SIX
HTTP/1.1 connections per scheme+host+port. A timer-driven poll holds one of
the six for a round trip, many times a minute, in every window, whether or not
anything changed — and the seventh fetch queues behind the first six, across
windows, while the server's access log looks healthy. WebSockets are not
counted. So the client stops deciding when to ask: a producer notices a change
once per process and the bus pushes a frame to each subscribed document.
HTTP/1.1 keeps two jobs: GET a snapshot, POST a mutation.

THE SHAPE. One `Topic` per live fact. A topic knows how to build the body the
matching GET would answer (`snapshot`), optionally how to answer "what moved
since generation N" (`delta`), and what kind of producer wakes it:

  * ``event``    — an in-process write point calls `publish()` (tasks_watch's
                   bump, `jobs.upsert`, the sysmon tick).
  * ``write``    — a mutation route or thread sets the state and calls
                   `publish()`; one line at each existing write point.
  * ``polldiff`` — the state is written by another process or the OS, so the
                   bus runs ONE stat/diff loop per topic key, in this process,
                   only while something is subscribed to that key, and
                   publishes when the signature changes. One process paying
                   one stat per interval instead of N windows paying one
                   request per interval.

THE CONTRACT (D3, D4, D5):

  * A subscribe always answers with a snapshot before any delta. The bus
    schedules that snapshot itself; producers cannot send.
  * Deltas are a topic's option, not a rule. A topic without `delta` pushes a
    fresh snapshot on change, coalesced latest-wins.
  * At most ONE unsent frame per subscription. A newer snapshot replaces an
    older unsent one; a newer delta is computed from the last generation the
    client was actually SENT, so it is a superset of the pending one and
    replaces it; a delta window the topic cannot answer degrades to a
    snapshot. A wedged client costs the server O(subscriptions), not O(events).
  * `publish()` is safe from any thread: the bus hands it to the one uvicorn
    loop with `call_soon_threadsafe`, as `tasks_watch._bump` always has.
  * A poll-and-diff producer exists only while this process has a subscriber
    for its key; the task is cancelled at zero.

Wire frames (JSON text) are documented on the route (`routers/events.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Hashable

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("fused_render")

# A process restart makes every generation meaningless; the client reads this
# off `hello` and resubscribes everything when it changes.
BOOT_ID = uuid.uuid4().hex

PING_EVERY_S = 15.0
# How long a polldiff signature read may take before the tick is skipped
# (never stacked: a hung read guards the next tick, as `_WatchEntry._read`).
POLL_READ_TIMEOUT_S = 10.0
# A pushed-delta list longer than this degrades the pending frame to a
# snapshot (D5): the client has fallen far enough behind that a full read is
# cheaper than replaying what it missed.
MAX_PENDING_CHANGES = 200
# A pulled delta the topic cannot answer at the client's generation (the
# snapshot builder behind it has not caught up yet) is retried this often,
# and after this many rounds the subscriber gets a snapshot instead.
DELTA_RETRY_S = 0.5
DELTA_RETRY_MAX = 20

# What a topic's `delta` returns to say "the change is real but the body
# behind it is not built yet — ask me again shortly" (the tasks listing's
# snapshot builder catching up with the watcher).
RETRY = object()
_UNSET_SIG = object()


class TopicError(Exception):
    """The GET's refusal, carried to the socket as an `err` frame."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def canonical(params: dict) -> str:
    """One string per distinct parameter set, whatever the key order."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


class Topic:
    """One live fact. Subclass, or build one with `simple_topic` below.

    Everything that reads disk or blocks runs OFF the event loop: `snapshot`,
    `delta` and `signature` are plain sync functions the bus runs in the
    threadpool. `validate` is sync too and may read disk (`scope=app` resolves
    a page to its app folder), so it rides the threadpool as well.
    """

    name: str = ""
    kind: str = "event"           # "event" | "write" | "polldiff"
    hidden_ok: bool = True        # may a hidden document drop it (D7)
    poll_interval_s: float | None = None   # polldiff only
    # When a polldiff topic's signature IS the snapshot body, the producer's
    # read doubles as the body every subscriber gets — one read, not two.
    signature_is_snapshot: bool = False

    def validate(self, params: dict) -> dict:
        """Normalize `params`, or raise `TopicError` as the GET would 400."""
        return dict(params or {})

    def key(self, params: dict) -> Hashable:
        """Which subscribers share one producer and one snapshot build."""
        return canonical(params)

    def snapshot(self, params: dict) -> dict:
        raise NotImplementedError

    def generation(self, body: dict) -> int | None:
        """The generation a snapshot body stands for, if the topic has one."""
        return None

    # Optional: `(body, gen)` for what moved since `since`; None when the
    # window cannot be answered (send a snapshot); `RETRY` when the change is
    # real but its body is not built yet; a `gen` not past `since` means
    # nothing moved and no frame is sent.
    delta: Callable[[dict, int], Any] | None = None

    def signature(self, params: dict) -> Any:
        """polldiff: the value whose change means "publish". Defaults to the
        snapshot itself (and then `signature_is_snapshot` should be True)."""
        return self.snapshot(params)

    # A producer that cannot read one signature for every subscriber (the
    # bots feed: each subscriber's answer depends on its own cursors) ticks
    # instead: every `poll_interval` it wakes every subscriber of the key, and
    # each one's delta/snapshot is built and then DROPPED when it says nothing
    # new (`frame_signature`). The wire carries only change.
    tick_only: bool = False

    def frame_signature(self, body: dict) -> Any:
        """What makes two consecutive frames for one subscriber "the same":
        a frame whose signature equals the last one sent is not sent. The
        default is the whole body; a topic whose body carries a clock
        (`ts`) excludes it."""
        return canonical(body)

    def poll_interval(self, params: dict, age_s: float, last: Any) -> float:
        """The producer's next sleep: `poll_interval_s` by default. A topic
        whose old client cadence depended on state (fast while a login is
        open, fast for the first second of an install) answers from `age_s`
        (seconds since the key's first subscriber) and `last` (the last
        signature) — the OLD CLIENT CADENCE EXACTLY, never a floor (D16)."""
        return float(self.poll_interval_s or 1.0)

    async def attach(self, key: Hashable, params: dict, bus: "EventBus") -> Callable[[], None] | None:
        """event topics with a per-key producer that must be started on the
        first subscriber (fs.watch's registry entry): return a detach callable,
        or None when the topic needs no per-key producer."""
        return None


def simple_topic(name: str, snapshot: Callable[[dict], dict], *, kind: str = "write",
                 hidden_ok: bool = True, poll_interval_s: float | None = None,
                 validate: Callable[[dict], dict] | None = None,
                 signature: Callable[[dict], Any] | None = None,
                 key: Callable[[dict], Hashable] | None = None) -> Topic:
    """A topic whose snapshot is the GET handler's inner function.

    For ``kind="polldiff"`` with no `signature`, the producer reads the
    snapshot every `poll_interval_s` and publishes when its JSON changes —
    the honest producer for state written by another process or the OS.
    """
    t = Topic()
    t.name = name
    t.kind = kind
    t.hidden_ok = hidden_ok
    t.poll_interval_s = poll_interval_s
    t.snapshot = snapshot  # type: ignore[method-assign]
    if validate is not None:
        t.validate = validate  # type: ignore[method-assign]
    if key is not None:
        t.key = key  # type: ignore[method-assign]
    if signature is not None:
        t.signature = signature  # type: ignore[method-assign]
    else:
        t.signature_is_snapshot = True
    if kind == "polldiff" and poll_interval_s is None:
        raise ValueError(f"polldiff topic {name!r} needs poll_interval_s")
    return t


# ----------------------------------------------------------------- frames

class _Frame:
    __slots__ = ("kind", "body", "gen", "changes", "raw")

    def __init__(self, kind: str, body: dict | None, gen: int | None, changes: list | None = None,
                 raw: str | None = None):
        self.kind = kind          # "snap" | "delta" | "err"
        self.body = body
        self.gen = gen
        self.changes = changes    # pushed-delta accumulation (fs.watch)
        # The body already serialized, ONCE per key per version, shared by
        # every subscriber of the key: a 1.7 MB tasks listing is not encoded
        # per window.
        self.raw = raw


class _Sub:
    """One subscription of one connection to one topic key."""

    def __init__(self, conn: "Connection", sid: Any, topic: Topic, params: dict, key: Hashable, since: int | None):
        self.conn = conn
        self.id = sid
        self.topic = topic
        self.params = params
        self.key = key
        # The generation the client was last SENT (or claimed on subscribe).
        self.gen: int | None = since
        self.refreshing: asyncio.Task | None = None
        self.dirty = False
        self.closed = False
        self.retries = 0
        self.claimed: int | None = None
        # The signature of the last frame SENT (not merely built), so an
        # unchanged body is never put on the wire twice.
        self.last_sig: Any = _UNSET_SIG


class _KeyState:
    """Everything the bus holds per (topic, key): the subscribers sharing it,
    the memoized snapshot build for the current version, and the producer."""

    def __init__(self, topic: Topic, key: Hashable, params: dict):
        self.topic = topic
        self.key = key
        self.params = params
        self.subs: set[_Sub] = set()
        self.version = 0
        self.build: asyncio.Future | None = None
        self.build_version = -1
        self.producer: asyncio.Task | None = None
        self.detach: Callable[[], None] | None = None
        # "Read again NOW": set by a publish on this key and by a subscriber
        # joining a key that already has a producer (a resync from a second
        # document), so a producer sleeping at its idle cadence re-reads and
        # re-chooses its interval at once instead of one idle interval later.
        self.poke = asyncio.Event()
        # A polldiff producer whose signature is the snapshot hands the body
        # straight in here, so the subscribers' refresh never rebuilds it.
        self.ready_body: dict | None = None
        self.ready_version = -1
        self.ready_raw: str | None = None


class Connection:
    """One socket: its subscriptions, its bounded pending frames, its sender."""

    def __init__(self, bus: "EventBus", send_text: Callable[[str], Any]):
        self.bus = bus
        self._send_text = send_text
        self.subs: dict[Any, _Sub] = {}
        self.pending: dict[Any, _Frame] = {}
        self.order: list[Any] = []
        self.wake = asyncio.Event()
        self.open = True
        self.sender: asyncio.Task | None = None
        self.last_sent = time.monotonic()

    def start(self) -> None:
        self.sender = asyncio.create_task(self._run())

    def enqueue(self, sub: _Sub, frame: _Frame) -> None:
        if not self.open or sub.closed:
            return
        prior = self.pending.get(sub.id)
        if prior is not None and prior.kind == "snap" and frame.kind == "delta":
            # A snapshot is still waiting to go out: it will be rebuilt to
            # include this change rather than followed by a delta it already
            # carries (the snapshot is read after the subscribe, the delta
            # describes a change after that read — a fresh read covers both).
            self.bus._kick(sub)
            return
        if prior is not None and prior.kind == "delta" and frame.kind == "delta" and frame.changes is not None:
            # Pushed deltas merge; past the bound the entry degrades to a
            # snapshot (D5) rather than growing without limit.
            merged = (prior.changes or []) + frame.changes
            if len(merged) > MAX_PENDING_CHANGES:
                self.pending[sub.id] = _Frame("snap", None, None)
                self.bus._kick(sub)
            else:
                prior.changes = merged
                prior.body = {"changes": merged}
                prior.gen = frame.gen
            return
        if prior is None:
            self.order.append(sub.id)
        self.pending[sub.id] = frame
        self.wake.set()

    async def _run(self) -> None:
        try:
            while self.open:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=PING_EVERY_S)
                except asyncio.TimeoutError:
                    if time.monotonic() - self.last_sent >= PING_EVERY_S:
                        await self._send({"t": "ping"})
                    continue
                self.wake.clear()
                while self.open and self.order:
                    sid = self.order.pop(0)
                    frame = self.pending.pop(sid, None)
                    sub = self.subs.get(sid)
                    if frame is None or sub is None or sub.closed:
                        continue
                    if frame.kind == "snap" and frame.body is None:
                        # A degraded entry: build the snapshot now.
                        self.bus._kick(sub)
                        continue
                    if frame.raw is not None:
                        head = '{"t":%s,"id":%s' % (json.dumps(frame.kind), json.dumps(sid))
                        if frame.gen is not None:
                            head += ',"gen":%d' % frame.gen
                        if not await self._send_raw(head + ',"body":' + frame.raw + "}"):
                            return
                    else:
                        msg: dict = {"t": frame.kind, "id": sid}
                        if frame.gen is not None:
                            msg["gen"] = frame.gen
                        if frame.kind == "err":
                            msg.update(frame.body or {})
                        else:
                            msg["body"] = frame.body
                        if not await self._send(msg):
                            return
                    if frame.gen is not None and frame.kind != "err":
                        sub.gen = frame.gen
        except asyncio.CancelledError:
            pass

    async def _send(self, msg: dict) -> bool:
        return await self._send_raw(json.dumps(msg, default=str))

    async def _send_raw(self, text: str) -> bool:
        try:
            await self._send_text(text)
        except Exception:  # noqa: BLE001 — the document went away mid-send
            self.open = False
            return False
        self.last_sent = time.monotonic()
        return True

    async def send_now(self, msg: dict) -> bool:
        """Out-of-band frames (`hello`), sent ahead of the queue."""
        return await self._send(msg)

    def close(self) -> None:
        self.open = False
        for sub in list(self.subs.values()):
            self.bus.unsubscribe(sub)
        self.subs.clear()
        self.pending.clear()
        self.order.clear()
        if self.sender is not None:
            self.sender.cancel()
        self.wake.set()


# -------------------------------------------------------------------- the bus

class EventBus:
    def __init__(self):
        self.topics: dict[str, Topic] = {}
        self._keys: dict[tuple[str, Hashable], _KeyState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.connections: set[Connection] = set()

    # ---- registry
    def register(self, topic: Topic) -> Topic:
        if not topic.name:
            raise ValueError("a topic needs a name")
        self.topics[topic.name] = topic
        return topic

    def topic(self, name: str) -> Topic | None:
        return self.topics.get(name)

    def catalog(self) -> dict:
        """What `hello` tells the client about every topic (the hidden policy
        lives client-side, D7, so it has to know which topics may be dropped)."""
        return {name: {"hidden_ok": bool(t.hidden_ok), "kind": t.kind}
                for name, t in self.topics.items()}

    # ---- loop binding
    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Capture the uvicorn loop so `publish` can be called from any thread.
        Called at startup and, as a fallback, on the first connection."""
        with self._lock:
            self._loop = loop or asyncio.get_running_loop()

    def reset(self) -> None:
        """Tests: forget every subscriber and producer (the registry stays)."""
        for conn in list(self.connections):
            conn.close()
        self.connections.clear()
        for state in list(self._keys.values()):
            self._stop_producer(state)
        self._keys.clear()
        with self._lock:
            self._loop = None

    # ---- connections
    def connect(self, send_text: Callable[[str], Any]) -> Connection:
        # Rebound on EVERY connection: under uvicorn it is the same loop the
        # startup hook captured; under a TestClient each `with` block runs its
        # own portal loop, and a publish aimed at a finished one would never
        # land.
        self.bind()
        conn = Connection(self, send_text)
        self.connections.add(conn)
        conn.start()
        return conn

    def disconnect(self, conn: Connection) -> None:
        conn.close()
        self.connections.discard(conn)

    # ---- subscriptions
    async def subscribe(self, conn: Connection, sid: Any, name: str, params: dict | None,
                        since: int | None = None) -> _Sub:
        """Register and schedule the snapshot. Raises `TopicError`."""
        topic = self.topics.get(name)
        if topic is None:
            raise TopicError(f"unknown topic {name!r}", 404)
        if not isinstance(params, dict):
            params = {}
        normalized = await run_in_threadpool(topic.validate, params)
        key = topic.key(normalized)
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            since = None
        # SNAPSHOT FIRST, ALWAYS (D4): `since` is what the client last held,
        # kept so the first delta after the snapshot can be bounded by it when
        # the snapshot itself stands for an older generation; the opening
        # frame is the GET's body whatever `since` says.
        sub = _Sub(conn, sid, topic, normalized, key, None)
        sub.claimed = since
        state = self._keys.get((name, key))
        if state is None:
            state = _KeyState(topic, key, normalized)
            self._keys[(name, key)] = state
            await self._start_producer(state)
        else:
            state.poke.set()
        state.subs.add(sub)
        conn.subs[sid] = sub
        self._kick(sub)
        return sub

    def unsubscribe(self, sub: _Sub) -> None:
        if sub.closed:
            return
        sub.closed = True
        sub.conn.subs.pop(sub.id, None)
        sub.conn.pending.pop(sub.id, None)
        if sub.refreshing is not None:
            sub.refreshing.cancel()
        state = self._keys.get((sub.topic.name, sub.key))
        if state is None:
            return
        state.subs.discard(sub)
        if not state.subs:
            self._stop_producer(state)
            self._keys.pop((sub.topic.name, sub.key), None)

    def subscriber_count(self, name: str, key: Hashable | None = None) -> int:
        if key is None:
            return sum(len(s.subs) for (n, _k), s in self._keys.items() if n == name)
        state = self._keys.get((name, key))
        return len(state.subs) if state else 0

    def producer_tasks(self) -> int:
        """Tests: how many poll-and-diff producers are alive right now."""
        return sum(1 for s in self._keys.values() if s.producer is not None and not s.producer.done())

    # ---- publish (any thread)
    def publish(self, name: str, key: Hashable | None = None, *, keys=None) -> None:
        """"It changed." From any thread. `key=None` wakes every key of the
        topic; `keys` names several at once."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._publish_on_loop, name, key, keys)
        except RuntimeError:
            pass  # the loop has closed (shutdown, a finished TestClient)

    def push_delta(self, name: str, key: Hashable, body: dict, gen: int | None = None) -> None:
        """A producer handing a ready-made delta to every subscriber of a key
        (fs.watch). From any thread. Merged per pending frame, bounded (D5)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._push_delta_on_loop, name, key, body, gen)
        except RuntimeError:
            pass

    def _states_for(self, name: str, key: Hashable | None, keys) -> list[_KeyState]:
        if keys is not None:
            wanted = set(keys)
            return [s for (n, k), s in self._keys.items() if n == name and k in wanted]
        if key is None:
            return [s for (n, _k), s in self._keys.items() if n == name]
        state = self._keys.get((name, key))
        return [state] if state else []

    def _publish_on_loop(self, name: str, key: Hashable | None, keys) -> None:
        for state in self._states_for(name, key, keys):
            state.version += 1
            state.ready_body = None
            state.ready_raw = None
            state.poke.set()
            for sub in list(state.subs):
                self._kick(sub)

    def _push_delta_on_loop(self, name: str, key: Hashable, body: dict, gen: int | None) -> None:
        state = self._keys.get((name, key))
        if state is None:
            return
        changes = body.get("changes") if isinstance(body, dict) else None
        for sub in list(state.subs):
            if sub.refreshing is not None and not sub.refreshing.done():
                # The snapshot has not gone out yet: rebuild it after this
                # change rather than queueing a delta it would already carry.
                self._kick(sub)
                continue
            frame = _Frame("delta", dict(body), gen, list(changes) if isinstance(changes, list) else None)
            sub.conn.enqueue(sub, frame)

    def _ready(self, state: _KeyState, body: dict) -> None:
        """A polldiff producer whose read IS the snapshot: memoize it for this
        version and wake the subscribers."""
        state.version += 1
        state.ready_body = body
        state.ready_version = state.version
        state.ready_raw = None
        for sub in list(state.subs):
            self._kick(sub)

    # ---- the refresh: one task per subscription, coalesced
    def _kick(self, sub: _Sub) -> None:
        if sub.closed:
            return
        if sub.refreshing is not None and not sub.refreshing.done():
            sub.dirty = True
            return
        sub.dirty = False
        sub.refreshing = asyncio.create_task(self._refresh(sub))

    async def _refresh(self, sub: _Sub) -> None:
        try:
            while True:
                sub.dirty = False
                try:
                    frame = await self._build(sub)
                except TopicError as exc:
                    frame = _Frame("err", {"status": exc.status, "error": str(exc)}, None)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — the GET would have 500ed
                    logger.exception("events: building %s for %r failed", sub.topic.name, sub.params)
                    frame = _Frame("err", {"status": 500, "error": "internal error"}, None)
                if sub.closed:
                    return
                if frame is not None:
                    sub.conn.enqueue(sub, frame)
                if not sub.dirty:
                    return
        except asyncio.CancelledError:
            pass

    async def _build(self, sub: _Sub) -> _Frame | None:
        topic = sub.topic
        state = self._keys.get((topic.name, sub.key))
        if state is None:
            return None
        if topic.delta is not None and sub.gen is not None:
            answer = await run_in_threadpool(topic.delta, sub.params, sub.gen)
            if answer is RETRY:
                # The builder behind the topic has not caught up with the
                # change this is about: ask again shortly, and after enough
                # rounds fall through to a snapshot.
                sub.retries += 1
                if sub.retries <= DELTA_RETRY_MAX:
                    await asyncio.sleep(DELTA_RETRY_S)
                    sub.dirty = True
                    return None
            elif answer is not None:
                body, gen = answer
                sub.retries = 0
                if isinstance(gen, int) and isinstance(sub.gen, int) and gen <= sub.gen:
                    return None  # nothing moved since what the client holds
                if self._same_as_last(sub, body):
                    sub.gen = gen
                    return None
                return _Frame("delta", body, gen)
        sub.retries = 0
        body, raw = await self._snapshot(state)
        if sub.gen is not None and self._same_as_last(sub, body):
            return None  # a re-tick that found nothing new for this subscriber
        if sub.gen is None:
            try:
                sub.last_sig = topic.frame_signature(body)
            except Exception:  # noqa: BLE001
                sub.last_sig = _UNSET_SIG
        return _Frame("snap", body, topic.generation(body), raw=raw)

    @staticmethod
    def _same_as_last(sub: _Sub, body: dict) -> bool:
        try:
            sig = sub.topic.frame_signature(body)
        except Exception:  # noqa: BLE001 — an unsignable body is always news
            return False
        if sub.last_sig is not _UNSET_SIG and sig == sub.last_sig:
            return True
        sub.last_sig = sig
        return False

    async def _snapshot(self, state: _KeyState) -> tuple[dict, str]:
        """The body for the current version and its JSON, built once per key
        per version however many subscribers share it."""
        if state.ready_body is not None and state.ready_version == state.version:
            if state.ready_raw is None:
                state.ready_raw = await run_in_threadpool(_dumps, state.ready_body)
            return state.ready_body, state.ready_raw
        if state.build is not None and state.build_version == state.version:
            return await asyncio.shield(state.build)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        state.build = fut
        state.build_version = state.version
        try:
            answer = await run_in_threadpool(_build_snapshot, state.topic, state.params)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            if state.build is fut:
                state.build = None
            raise
        if not fut.done():
            fut.set_result(answer)
        return answer

    # ---- producers
    async def _start_producer(self, state: _KeyState) -> None:
        topic = state.topic
        # A producer loop for any topic that names an interval: polldiff by
        # definition, and the event/write topics whose state also moves on a
        # clock nobody writes through (a job ageing out, a worker's health).
        if topic.poll_interval_s:
            state.producer = asyncio.create_task(self._poll_loop(state))
        detach = await topic.attach(state.key, state.params, self)
        if detach is not None:
            state.detach = detach

    def _stop_producer(self, state: _KeyState) -> None:
        if state.producer is not None:
            state.producer.cancel()
            state.producer = None
        if state.detach is not None:
            try:
                state.detach()
            except Exception:  # noqa: BLE001
                logger.exception("events: detaching %s failed", state.topic.name)
            state.detach = None

    async def _poll_loop(self, state: _KeyState) -> None:
        """One stat/diff loop per key, off-loop with a timeout, publishing only
        when the signature changes. The first read primes the baseline; the
        subscribe's own snapshot already told the client where things stand."""
        topic = state.topic
        last: Any = _UNSET
        last_sig: Any = None
        inflight: asyncio.Future | None = None
        started = time.monotonic()
        # The key version this loop's baseline stands for: a publish since
        # then (a write hook) already pushed the change this read is about to
        # find, so the read re-baselines without pushing it a second time.
        seen_version = state.version
        try:
            if topic.tick_only:
                while True:
                    await _nap(state, topic.poll_interval(state.params, time.monotonic() - started, last_sig))
                    self._publish_on_loop(topic.name, state.key, None)
            while True:
                sig: Any = _UNSET
                if inflight is not None:
                    if inflight.done():
                        try:
                            sig = inflight.result()
                        except Exception:  # noqa: BLE001
                            sig = _UNSET
                        inflight = None
                else:
                    inflight = asyncio.ensure_future(run_in_threadpool(topic.signature, state.params))
                    try:
                        sig = await asyncio.wait_for(asyncio.shield(inflight), POLL_READ_TIMEOUT_S)
                        inflight = None
                    except asyncio.TimeoutError:
                        sig = _UNSET
                    except Exception:  # noqa: BLE001 — a read that failed is "unchanged"
                        inflight = None
                        sig = _UNSET
                if sig is not _UNSET:
                    marker = canonical(sig) if isinstance(sig, (dict, list)) else sig
                    published_since = state.version != seen_version
                    if last is not _UNSET and marker != last and not published_since:
                        if topic.signature_is_snapshot and isinstance(sig, dict):
                            self._ready(state, sig)
                        else:
                            self._publish_on_loop(topic.name, state.key, None)
                    seen_version = state.version
                    last = marker
                    last_sig = sig
                await _nap(state, topic.poll_interval(state.params, time.monotonic() - started, last_sig))
        except asyncio.CancelledError:
            pass


async def _nap(state: "_KeyState", seconds: float) -> None:
    """The producer's sleep, cut short by a poke on its key (a publish, or a
    second document resubscribing): a write hook or a resync means "look
    now", and the cadence the next read chooses (1.5 s while a login is open,
    not 60 s) starts from that read."""
    state.poke.clear()
    try:
        await asyncio.wait_for(state.poke.wait(), timeout=max(0.0, float(seconds)))
    except asyncio.TimeoutError:
        pass


_UNSET = object()


def _dumps(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"), default=str, ensure_ascii=False)


def _build_snapshot(topic: Topic, params: dict) -> tuple[dict, str]:
    body = topic.snapshot(params)
    return body, _dumps(body)


bus = EventBus()
