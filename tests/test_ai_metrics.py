"""Tests for the /api/ai token counter (SPEC AI-12): the in-memory ring in
`server/ai_metrics.py`, the four terminal frames that feed it, and the
`GET /api/ai/metrics` read the Usage tab polls.

The store's clocks are injectable (`_Store(monotonic=..., wall=...)`), so the
bucket tests walk an hour of the ring in a few lines without moving the
machine's clock or patching `time` out from under asyncio.

The Claude tier is driven through `_ai_relay` with the CLI process faked —
test_server_ai.py's harness, imported rather than rebuilt, so these tests
record from exactly the code path that file already pins. The local tier needs
no runner at all: `_local_relay` reaches the supervisor through one generator
function, and that is what is replaced here.
"""
import asyncio
import json
import tempfile

import pytest
from fastapi.testclient import TestClient

from fused_render.ai import supervisor
from fused_render.server import ai as _server_ai
from fused_render.server import ai_metrics
from fused_render.server.app import create_app

from test_server_ai import _CLI_RESULT, _cli_ok, _result_lines


class _Clock:
    """A monotonic clock a test moves by hand. Wall time is derived from it so
    the two never disagree — which is what lets a test assert a bucket's `t`."""

    def __init__(self, wall_start=1_700_000_000.0):
        self.t = 0.0
        self._wall_start = wall_start

    def monotonic(self):
        return self.t

    def wall(self):
        return self._wall_start + self.t

    def advance(self, seconds):
        self.t += seconds


def _store(clock):
    return ai_metrics._Store(monotonic=clock.monotonic, wall=clock.wall)


@pytest.fixture(autouse=True)
def _empty_counter():
    """Every test starts from an empty process counter, and leaves one behind:
    the module-level store outlives a test the way it outlives a request."""
    ai_metrics.reset()
    yield
    ai_metrics.reset()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir=tempfile.mkdtemp()))


# -- the ring ------------------------------------------------------------------


def test_a_completion_lands_in_the_bucket_it_happened_in():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"input_tokens": 3, "output_tokens": 7})
    clock.advance(25)  # two buckets later
    store.record("haiku", {"input_tokens": 1, "output_tokens": 5})

    buckets = store.snapshot(1)["buckets"]
    # Dense and oldest-first: three buckets, with the untouched middle one
    # present and zero — the gap is the information a bar chart draws.
    assert [b["output_tokens"] for b in buckets] == [7, 0, 5]
    assert [b["completions"] for b in buckets] == [1, 0, 1]


def test_buckets_are_stamped_with_wall_time_one_width_apart():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"output_tokens": 1})
    clock.advance(3 * ai_metrics.BUCKET_S)

    snap = store.snapshot(1)
    stamps = [b["t"] for b in snap["buckets"]]
    assert stamps == sorted(stamps)
    for earlier, later in zip(stamps, stamps[1:]):
        assert round(later - earlier, 6) == ai_metrics.BUCKET_S
    # The newest bucket is the one `now` falls inside, never one in the future.
    assert stamps[-1] <= snap["now"] < stamps[-1] + ai_metrics.BUCKET_S


def test_nothing_is_emitted_for_time_before_counting_began():
    """A server two minutes old has two minutes of graph, not an hour of zeros
    claiming it generated nothing at breakfast."""
    clock = _Clock()
    store = _store(clock)
    clock.advance(120)
    # Twelve finished buckets plus the one now is inside.
    assert len(store.snapshot(60)["buckets"]) == 120 // ai_metrics.BUCKET_S + 1


def test_a_bucket_older_than_the_ring_leaves_the_graph_but_not_the_totals():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"output_tokens": 9})
    clock.advance(ai_metrics.WINDOW_S + ai_metrics.BUCKET_S)
    store.record("haiku", {"output_tokens": 4})

    snap = store.snapshot(ai_metrics.MAX_MINUTES)
    assert sum(b["output_tokens"] for b in snap["buckets"]) == 4
    # The ring is a graph, not a ledger: what fell off it is still in the
    # since-start totals, which is the number the tiles read.
    assert snap["totals"]["output_tokens"] == 13
    assert snap["totals"]["completions"] == 2


def test_a_lap_of_the_ring_overwrites_rather_than_accumulates():
    """The slot for 10:00:00 is the slot for 11:00:00. A slot that added to
    counts from the previous lap would report an hour-old spike as current."""
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"output_tokens": 100})
    clock.advance(ai_metrics.WINDOW_S)  # exactly one lap: same slot, next key
    store.record("haiku", {"output_tokens": 1})
    assert store.snapshot(1)["buckets"][-1]["output_tokens"] == 1


def test_the_window_summarises_exactly_the_buckets_it_returns():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"input_tokens": 2, "output_tokens": 10})
    clock.advance(600)  # ten minutes on
    store.record("haiku", {"input_tokens": 1, "output_tokens": 3})

    five = store.snapshot(5)
    assert five["window"]["output_tokens"] == 3
    assert five["window"]["output_tokens"] == sum(
        b["output_tokens"] for b in five["buckets"])
    assert five["totals"]["output_tokens"] == 13


# -- what a number means, and what a missing one means -------------------------


def test_input_tokens_are_null_when_the_tier_never_reported_them():
    """A local worker counts what it GENERATED and never sees a prompt token
    (AI-3). Summing that absence as 0 would put "0 read" in a table, which is
    an answer where there is none."""
    clock = _Clock()
    store = _store(clock)
    store.record("org/local", {"output_tokens": 5, "seconds": 0.4})

    snap = store.snapshot(5)
    row, = snap["models"]
    assert (row["model"], row["input_tokens"], row["output_tokens"]) \
        == ("org/local", None, 5)
    assert snap["totals"]["input_tokens"] is None
    assert snap["window"]["input_tokens"] is None


def test_a_reported_zero_is_a_number_not_an_absence():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"input_tokens": 0, "output_tokens": 0})
    assert store.snapshot(5)["totals"]["input_tokens"] == 0


def test_a_completion_with_no_usage_at_all_still_counts_as_one():
    """It happened. A graph that dropped it would draw an idle machine while a
    model was talking."""
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", None)
    snap = store.snapshot(5)
    assert snap["totals"]["completions"] == 1
    assert snap["totals"]["output_tokens"] == 0


@pytest.mark.parametrize("usage", [
    {"output_tokens": True},        # a bool is an int in Python; not one token
    {"output_tokens": -5},
    {"output_tokens": "twelve"},
    {"output_tokens": 3.5},
    {"output_tokens": None},
    "not a dict",
])
def test_a_count_this_module_cannot_trust_is_not_summed(usage):
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", usage)
    snap = store.snapshot(5)
    assert snap["totals"]["output_tokens"] == 0
    assert snap["totals"]["completions"] == 1


def test_models_past_the_cap_fold_into_one_row_and_the_totals_stay_right():
    clock = _Clock()
    store = _store(clock)
    for i in range(ai_metrics.MAX_MODELS + 5):
        store.record(f"org/model-{i}", {"output_tokens": 2})

    snap = store.snapshot(5)
    # The cap counts NAMED models; the overflow row is the one extra.
    assert len(snap["models"]) == ai_metrics.MAX_MODELS + 1
    overflow, = [m for m in snap["models"] if m["model"] == ai_metrics.OTHER_MODEL]
    assert overflow["completions"] == 5
    assert snap["totals"]["output_tokens"] == (ai_metrics.MAX_MODELS + 5) * 2
    # A space is what keeps the overflow row unimpersonable: `_AI_MODEL_RE`
    # admits none, so no real model can be merged into it by name.
    assert not _server_ai._AI_MODEL_RE.fullmatch(ai_metrics.OTHER_MODEL)


def test_models_are_biggest_generator_first():
    clock = _Clock()
    store = _store(clock)
    store.record("small", {"output_tokens": 1})
    store.record("big", {"output_tokens": 900})
    assert [m["model"] for m in store.snapshot(5)["models"]] == ["big", "small"]


@pytest.mark.parametrize("asked,served", [
    (0, 1.0), (-5, 1.0), (999, 60.0), ("nonsense", 15.0), (None, 15.0),
    (float("nan"), 15.0), (5, 5.0),
])
def test_the_window_is_clamped_not_refused(asked, served):
    """A graph asking for two hours from a store that keeps one should get the
    hour, not a 400."""
    clock = _Clock()
    store = _store(clock)
    assert store.snapshot(asked)["window_minutes"] == served


def test_record_never_raises_whatever_it_is_handed():
    """A counter may not break the completion it is counting."""
    ai_metrics.record(None, {"output_tokens": 1})
    ai_metrics.record("haiku", ["not", "a", "dict"])
    assert ai_metrics.snapshot(5)["totals"]["completions"] == 2


# -- failures, speed and tiers -------------------------------------------------


def test_a_failure_is_counted_but_is_not_a_completion():
    """"44 completions" must not include three calls that never answered."""
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"output_tokens": 5})
    store.record_failure("haiku", "timeout")
    store.record_failure("haiku", "timeout")
    store.record_failure("org/chat", "model_loading")

    snap = store.snapshot(5)
    assert snap["totals"]["completions"] == 1
    assert snap["totals"]["failures"] == 3
    assert snap["window"]["failures"] == 3
    assert sum(b["failures"] for b in snap["buckets"]) == 3
    # By kind, because "3 failed" and "3 timed out" send a user to different
    # places — and a model that is still loading is not a broken one.
    assert snap["failure_types"] == [{"type": "timeout", "count": 2},
                                     {"type": "model_loading", "count": 1}]
    by_model = {m["model"]: m["failures"] for m in snap["models"]}
    assert by_model == {"haiku": 2, "org/chat": 1}


def test_speed_divides_only_the_tokens_that_were_timed():
    """A cancelled local generation reports its tokens and no duration. Counting
    those tokens against the seconds of OTHER completions would report a speed
    the model never ran at."""
    clock = _Clock()
    store = _store(clock)
    store.record("org/chat", {"output_tokens": 100, "seconds": 10.0})
    store.record("org/chat", {"output_tokens": 40})  # cancelled: no seconds

    row, = store.snapshot(5)["models"]
    assert row["output_tokens"] == 140      # every token this machine made
    assert row["seconds"] == 10.0
    assert row["tokens_per_second"] == 10.0  # ...but only the timed ones divide


def test_speed_is_null_rather_than_zero_when_nothing_was_timed():
    clock = _Clock()
    store = _store(clock)
    store.record("haiku", {"output_tokens": 9})
    totals = store.snapshot(5)["totals"]
    assert totals["seconds"] is None and totals["tokens_per_second"] is None


def test_the_two_tiers_are_counted_apart_on_the_slash_seam():
    """`/api/ai` is one door with two tiers (AI-1), and this page's subject is
    the local half — a merged total would answer neither question."""
    clock = _Clock()
    store = _store(clock)
    store.record("claude-opus-5", {"input_tokens": 4, "output_tokens": 10})
    store.record("mlx-community/Qwen3-8B-4bit", {"output_tokens": 90, "seconds": 3.0})
    store.record_failure("mlx-community/Qwen3-8B-4bit", "model_loading")

    tiers = store.snapshot(5)["tiers"]
    assert tiers["claude"]["output_tokens"] == 10
    assert tiers["claude"]["failures"] == 0
    assert tiers["local"]["output_tokens"] == 90
    assert tiers["local"]["failures"] == 1
    assert tiers["local"]["tokens_per_second"] == 30.0
    assert [m["tier"] for m in store.snapshot(5)["models"]] == ["local", "claude"]


def test_a_local_model_past_the_cap_still_counts_as_local():
    """The overflow row's placeholder id has no slash, so a tier read AFTER the
    fold puts every local model past the cap in the Claude column — wrong on
    exactly the path the cap exists for."""
    clock = _Clock()
    store = _store(clock)
    for i in range(ai_metrics.MAX_MODELS):
        store.record(f"claude-model-{i}", {"output_tokens": 1})
    store.record("mlx-community/over-the-cap", {"output_tokens": 100})
    store.record_failure("mlx-community/over-the-cap", "ai_error")

    snap = store.snapshot(5)
    assert snap["tiers"]["local"]["output_tokens"] == 100
    assert snap["tiers"]["local"]["failures"] == 1
    assert snap["tiers"]["claude"]["output_tokens"] == ai_metrics.MAX_MODELS
    # The row itself names NO tier: it is a mixture by construction, and its id
    # cannot answer the question either way.
    overflow, = [m for m in snap["models"] if m["model"] == ai_metrics.OTHER_MODEL]
    assert overflow["tier"] is None
    assert overflow["output_tokens"] == 100


def test_the_snapshot_says_when_the_last_completion_landed():
    """"nothing in the last 15 minutes" and "nothing ever" look identical on an
    empty graph; this is what tells them apart."""
    clock = _Clock()
    store = _store(clock)
    assert store.snapshot(5)["last_completion_at"] is None
    store.record("haiku", {"output_tokens": 1})
    clock.advance(90)
    assert store.snapshot(5)["last_completion_at"] == round(clock.wall() - 90, 3)


# -- the four terminal frames --------------------------------------------------


def _local(monkeypatch, events):
    monkeypatch.setattr(supervisor, "generate_text",
                        lambda model, request: iter(events))


def test_the_claude_tier_records_what_the_caller_was_told(monkeypatch):
    _cli_ok(monkeypatch, _CLI_RESULT)
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hello"}))
    told = json.loads(bytes(resp.body))["result"]

    snap = ai_metrics.snapshot(5)
    assert snap["totals"]["completions"] == 1
    assert snap["totals"]["input_tokens"] == told["usage"]["input_tokens"]
    assert snap["totals"]["output_tokens"] == told["usage"]["output_tokens"]
    # The CLI's own duration, so the tokens/second figure is about the MODEL
    # and not about how long this relay held its subprocess.
    assert snap["totals"]["seconds"] == round(_CLI_RESULT["duration_ms"] / 1000, 2)
    # Under the RESOLVED id, so "haiku" and its full id are one row.
    assert [m["model"] for m in snap["models"]] == [told["model"]]


def test_the_claude_tier_records_a_streamed_completion_once(monkeypatch):
    _cli_ok(monkeypatch, lines=_result_lines(_CLI_RESULT, deltas=("hi ", "there")))

    async def go():
        resp = await _server_ai._ai_relay({"prompt": "hello", "stream": True})
        return [json.loads(line)
                for chunk in [c async for c in resp.body_iterator]
                for line in chunk.splitlines() if line]

    frames = asyncio.run(go())
    assert frames[-1]["ok"] is True
    snap = ai_metrics.snapshot(5)
    assert snap["totals"]["completions"] == 1
    assert snap["totals"]["output_tokens"] == _CLI_RESULT["usage"]["output_tokens"]


def test_a_failed_claude_call_is_a_failure_not_a_completion(monkeypatch):
    _cli_ok(monkeypatch, dict(_CLI_RESULT, is_error=True))
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hello"}))
    assert resp.status_code == 502
    snap = ai_metrics.snapshot(5)
    assert snap["totals"]["completions"] == 0
    assert snap["totals"]["failures"] == 1


def test_the_local_tier_records_its_generated_tokens(monkeypatch):
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "tokens": 12, "seconds": 0.5}])
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hi", "model": "org/chat"}))
    assert resp.status_code == 200

    row, = ai_metrics.snapshot(5)["models"]
    assert (row["model"], row["tier"], row["completions"],
            row["input_tokens"], row["output_tokens"]) \
        == ("org/chat", "local", 1, None, 12)
    # 12 tokens in half a second, as the worker reported both.
    assert row["seconds"] == 0.5 and row["tokens_per_second"] == 24.0


def test_the_local_tier_records_a_streamed_completion(monkeypatch):
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "tokens": 3, "seconds": 0.1}])

    async def go():
        resp = await _server_ai._ai_relay(
            {"prompt": "hi", "model": "org/chat", "stream": True})
        # Starlette runs a sync generator in a threadpool and hands back an
        # ASYNC iterator either way, so both tiers are drained the same.
        return [json.loads(line)
                for chunk in [c async for c in resp.body_iterator]
                for line in chunk.splitlines() if line]

    frames = asyncio.run(go())
    assert frames[-1] == {"type": "done", "ok": True, "result": {
        "text": "hi", "model": "org/chat",
        # `input_tokens` is present and null: this worker did not count the
        # prompt, and the key says so rather than being absent for one runner
        # and present for another.
        "usage": {"input_tokens": None, "output_tokens": 3, "seconds": 0.1}}}
    assert ai_metrics.snapshot(5)["totals"]["output_tokens"] == 3


def test_a_local_worker_that_counts_the_prompt_is_believed(monkeypatch):
    """The worker holds the tokenizer, so it is the only process that can say
    how long the prompt was in the model's own tokens (AI-3). Reported, it
    reaches both the caller's `usage` and the counter."""
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "tokens": 4,
                          "input_tokens": 37, "seconds": 0.2}])
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hi", "model": "org/chat"}))
    told = json.loads(bytes(resp.body))["result"]
    assert told["usage"]["input_tokens"] == 37

    row, = ai_metrics.snapshot(5)["models"]
    assert row["input_tokens"] == 37 and row["output_tokens"] == 4


def test_a_cancelled_local_generation_counts_the_tokens_it_made(monkeypatch):
    """Stop pressed mid-answer: the worker reports what it emitted (AI-1a), and
    this machine generated those tokens whether or not anyone wanted them."""
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "cancelled": True, "tokens": 2}])
    asyncio.run(_server_ai._ai_relay({"prompt": "hi", "model": "org/chat"}))
    assert ai_metrics.snapshot(5)["totals"]["output_tokens"] == 2


def test_a_failed_local_generation_is_a_failure_not_a_completion(monkeypatch):
    _local(monkeypatch, [{"type": "done", "ok": False, "error": "boom"}])
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hi", "model": "org/chat"}))
    assert resp.status_code == 502
    snap = ai_metrics.snapshot(5)
    assert snap["totals"]["completions"] == 0
    assert snap["failure_types"] == [{"type": "ai_error", "count": 1}]


def test_a_claude_timeout_is_counted_as_a_failure_of_its_kind(monkeypatch):
    _cli_ok(monkeypatch, hang=True)
    monkeypatch.setattr(_server_ai, "_AI_TIMEOUT_S", 0.05)
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hello"}))
    assert resp.status_code == 502

    snap = ai_metrics.snapshot(5)
    assert snap["totals"] == dict(snap["totals"], completions=0, failures=1)
    assert snap["failure_types"] == [{"type": "timeout", "count": 1}]


def test_a_missing_claude_binary_is_counted(monkeypatch):
    """The "why is nothing generating" case, and the one a graph of zero bars
    explains least well on its own."""
    monkeypatch.setattr(_server_ai.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hello"}))
    assert resp.status_code == 502
    assert ai_metrics.snapshot(5)["failure_types"] == \
        [{"type": "ai_unavailable", "count": 1}]


def test_a_model_that_is_still_loading_is_counted_as_its_own_kind(monkeypatch):
    """409 is not "broken": the call started a download and said so (AI-5). It
    still produced no text, so it is counted — under a type that says which."""
    def cold(model, request):
        raise supervisor.ModelNotReady("org/chat is loading now", "sys:ai-model:orgchat")
        yield  # pragma: no cover - generator function, never reached

    monkeypatch.setattr(supervisor, "generate_text", cold)
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "hi", "model": "org/chat"}))
    assert resp.status_code == 409

    snap = ai_metrics.snapshot(5)
    assert snap["failure_types"] == [{"type": "model_loading", "count": 1}]
    assert snap["tiers"]["local"]["failures"] == 1


def test_a_refused_request_never_reaches_the_counter(monkeypatch):
    """A malformed body is not a failure of the AI: nothing was asked of a
    model, and folding a typo into the failure rate would make the one number
    that means "the AI is not working" mean "somebody sent a bad request" too."""
    resp = asyncio.run(_server_ai._ai_relay({"prompt": "   "}))
    assert resp.status_code == 400
    snap = ai_metrics.snapshot(5)
    assert snap["totals"]["completions"] == 0
    assert snap["totals"]["failures"] == 0


# -- the route -----------------------------------------------------------------


def test_the_metrics_route_is_an_unguarded_read(client):
    """No `X-Fused`: it is a read, like every other read in this app (D3's
    guard is on the routes that spend the machine's time)."""
    ai_metrics.record("haiku", {"input_tokens": 4, "output_tokens": 6})
    body = client.get("/api/ai/metrics").json()
    assert body["totals"]["completions"] == 1
    assert body["totals"]["input_tokens"] == 4
    assert body["totals"]["output_tokens"] == 6
    assert body["bucket_seconds"] == ai_metrics.BUCKET_S
    assert body["retention_minutes"] == ai_metrics.MAX_MINUTES
    assert body["window_minutes"] == 15


def test_the_route_serves_the_window_it_was_asked_for(client):
    body = client.get("/api/ai/metrics?minutes=5").json()
    assert body["window_minutes"] == 5
    assert len(body["buckets"]) <= 5 * 60 // ai_metrics.BUCKET_S
    assert client.get("/api/ai/metrics?minutes=600").json()["window_minutes"] \
        == ai_metrics.MAX_MINUTES


def test_the_payload_says_when_this_process_started_counting(client):
    """Every figure is bounded by the process, so the payload has to carry the
    window it is true for — a restarted server reads as "counting since 09:14",
    never as a machine that generated nothing today."""
    body = client.get("/api/ai/metrics").json()
    assert body["since"] <= body["now"]
