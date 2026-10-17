"""fused_render/netlog.py: route normalisation, source naming, the request ring,
rollups, websockets, and the browser queue merge. A fake clock stands in for
time so no test sleeps."""
import pytest

from fused_render import netlog


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def log():
    c = Clock()
    lg = netlog.NetLog(clock=c, wall=lambda: 1_700_000_000 + c.t)
    lg.c = c
    return lg


def req(lg, path="/api/fs/list", query="", page=None, ua="Mozilla/5.0 AppleWebKit/605", addr="127.0.0.1",
        method="GET"):
    headers = {"user-agent": ua}
    if page:
        headers["x-fused-page"] = page
    return lg.begin(method, path, query, headers, addr)


def test_normalize_route_collapses_ids_and_strips_query():
    n = netlog.normalize_route
    assert n("/api/fs/list?path=/x") == "/api/fs/list"
    assert n("/api/tasks/123/stream") == "/api/tasks/{id}/stream"
    assert n("/api/terminal/deadbeef0123/stream") == "/api/terminal/{id}/stream"
    assert n("/api/jobs/123e4567-e89b-12d3-a456-426614174000") == "/api/jobs/{id}"
    assert n("/api/x/" + "a" * 41) == "/api/x/{id}"
    # words that merely look hex-ish stay
    assert n("/api/fs/facade") == "/api/fs/facade"
    assert n("/api/system/activity") == "/api/system/activity"


def test_full_path_keeps_keys_strips_values():
    assert netlog.full_path("/api/fs/raw", "path=/secret&x=1") == "/api/fs/raw?path&x"
    assert netlog.full_path("/api/fs/raw", "") == "/api/fs/raw"


def test_ua_family_and_client_key():
    assert netlog.ua_family("curl/8.1") == "curl"
    assert netlog.ua_family("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko)") == "WebKit app"
    assert netlog.ua_family("Mozilla/5.0 AppleWebKit/605 Version/17 Safari/605") == "Safari"
    assert netlog.ua_family("Mozilla/5.0 AppleWebKit/537 Chrome/120 Safari/537") == "Chrome"
    assert netlog.ua_family("Mozilla/5.0 Gecko/2010 Firefox/120") == "Firefox"
    assert netlog.ua_family("") == "other"
    assert netlog.client_key("127.0.0.1", "curl/8") == "127.0.0.1 curl"


def test_source_name(tmp_path):
    app = tmp_path / "myapp"
    app.mkdir()
    page = app / "view.html"
    page.write_text("x")
    assert netlog.source_name(None, None) == "other"
    assert netlog.source_name(str(page), None) in ("myapp", "view.html")
    assert netlog.source_name("/monitor", None) == "shell /monitor"
    assert netlog.source_name(None, "http://127.0.0.1:1777/monitor?tab=network") == "shell /monitor"
    # the shell addresses a file by its `path` param
    got = netlog.source_name(None, f"http://127.0.0.1:1777/?path={page}")
    assert got in ("myapp", "view.html")


def test_begin_finish_records_fields(log):
    r = req(log, "/api/fs/list", "path=/a", page="/monitor")
    assert r["route"] == "/api/fs/list" and r["path"] == "/api/fs/list?path"
    assert r["source"] == "shell /monitor"
    assert r["client"] == "127.0.0.1 WebKit app"
    log.c.t += 0.2
    log.finish(r, status=200, server_ms=12.34, nbytes=99)
    row = log.requests()["rows"][0]
    assert row["status"] == 200 and row["server_ms"] == 12.3 and row["bytes"] == 99
    assert row["outcome"] == "ok" and row["held_ms"] == 200.0


def test_finish_is_idempotent_and_500_is_error(log):
    r = req(log)
    log.finish(r, status=500)
    log.finish(r, status=200, outcome="disconnected")  # a late cancel after body end
    assert log.summary()["errors_60s"] == 1
    assert len(log.requests()["rows"]) == 1


def test_beacon_not_logged_and_self_flagged(log):
    assert req(log, netlog.BEACON_PATH, method="POST") is None
    s = req(log, "/api/system/dashboard")
    assert s["self"] is True
    log.finish(s, status=200)
    assert log.requests()["rows"] == []
    assert len(log.requests(include_self=True)["rows"]) == 1
    assert log.groups() == []


def test_ring_trims_by_time_and_cap(log, monkeypatch):
    r = req(log)
    log.finish(r, status=200)
    log.c.t += netlog.RETAIN_S + 1
    assert log.requests()["rows"] == []
    monkeypatch.setattr(netlog, "MAX_RECORDS", 3)
    for _ in range(5):
        log.finish(req(log), status=200)
    assert len(log.requests()["rows"]) == 3


def test_requests_cursor_returns_new_and_updated(log):
    a = req(log)
    first = log.requests()
    assert [r["outcome"] for r in first["rows"]] == [None]
    cur = first["cursor"]
    assert log.requests(cur)["rows"] == []
    log.finish(a, status=200)
    b = req(log)
    got = log.requests(cur)
    assert {r["id"] for r in got["rows"]} == {a["id"], b["id"]}
    assert next(r for r in got["rows"] if r["id"] == a["id"])["outcome"] == "ok"


def test_requests_filters(log):
    a = req(log, "/api/fs/list", page="/monitor")
    log.c.t += 1
    log.finish(a, status=200)
    b = req(log, "/api/run", page="/monitor", method="POST")
    log.finish(b, status=404)
    c = req(log, "/api/fs/raw")
    rows = lambda **kw: {r["id"] for r in log.requests(**kw)["rows"]}  # noqa: E731
    assert rows(source="shell /monitor") == {a["id"], b["id"]}
    assert rows(route="/api/run") == {b["id"]}
    assert rows(status="4xx") == {b["id"]}
    assert rows(min_ms=500) == {a["id"]}
    assert rows(open_only=True) == {c["id"]}


def test_summary_rates_inflight_and_saturation(log):
    for _ in range(20):
        log.finish(req(log), status=200)
        log.c.t += 0.5
    s = log.summary()
    assert s["rps_10s"] == 2.0   # 20 finished over 10 s, one per 0.5 s
    held = [req(log, "/api/fs/events-poll", page="/x/slow.html") for _ in range(7)]
    log.c.t += 3
    s = log.summary()
    assert s["in_flight_total"] == 7
    c = s["in_flight"][0]
    assert c["in_flight"] == 7 and c["saturated_s"] >= 3
    assert c["top"]["route"] == "/api/fs/events-poll" and c["top"]["count"] == 7
    assert s["oldest_in_flight_s"] >= 3
    assert held  # keep refs


def test_saturation_is_per_client(log):
    for _ in range(4):
        req(log, addr="127.0.0.1")
    for _ in range(4):
        req(log, addr="10.0.0.2")
    log.c.t += 5
    s = log.summary()
    assert len(s["in_flight"]) == 2
    assert all(c["saturated_s"] == 0 for c in s["in_flight"])  # none has 6 slots held


def test_long_held_lists_open_and_completed(log):
    done = req(log, "/api/a")
    open_ = req(log, "/api/b")
    log.c.t += 6
    log.finish(done, status=200)
    lh = log.summary()["long_held"]
    assert {r["route"] for r in lh} == {"/api/a", "/api/b"}
    assert open_


def test_groups_percentiles_errors_concurrency(log):
    for i in range(10):
        r = req(log, "/api/fs/stat", page="/monitor")
        log.c.t += 0.1
        log.finish(r, status=500 if i == 0 else 200, server_ms=(i + 1) * 10)
    g = log.groups(60)
    assert len(g) == 1
    row = g[0]
    assert row["source"] == "shell /monitor" and row["route"] == "/api/fs/stat"
    assert row["count"] == 10 and row["per_min"] == 10.0
    assert row["p50_ms"] == 55.0 and row["p95_ms"] == 95.5
    assert row["errors"] == 1 and row["max_in_flight"] == 1
    a, b = req(log, "/api/x"), req(log, "/api/x")
    log.finish(a, status=200)
    log.finish(b, status=200)
    assert next(g for g in log.groups() if g["route"] == "/api/x")["max_in_flight"] == 2


def test_groups_sorted_by_rate(log):
    for _ in range(3):
        log.finish(req(log, "/api/a"), status=200)
    for _ in range(5):
        log.finish(req(log, "/api/b"), status=200)
    assert [g["route"] for g in log.groups()] == ["/api/b", "/api/a"]


def test_client_report_merges_into_groups_and_summary(log):
    r = req(log, "/api/fs/stat", page="/monitor")
    log.finish(r, status=200)
    log.client_report("shell /monitor", {"/api/fs/stat": {
        "count": 4, "sum": 400, "max": 300, "samples": [10, 20, 30, 300]}})
    g = log.groups()[0]
    assert g["queue_max_ms"] == 300 and g["queue_p95_ms"] > 100
    assert log.summary()["browser_queue_p95_ms"] > 100
    log.c.t += netlog.CLIENT_STATS_S + 1
    assert log.summary()["browser_queue_p95_ms"] is None


def test_client_report_tolerates_garbage(log):
    log.client_report("x", {"/a": {"count": "nope"}, "/b": None})
    assert log.summary()["browser_queue_p95_ms"] is None


def test_client_report_normalises_route(log):
    r = req(log, "/api/jobs/123", page="/monitor")
    log.finish(r, status=200)
    log.client_report("shell /monitor", {"/api/jobs/999": {
        "count": 1, "sum": 5, "max": 5, "samples": [5]}})
    assert log.groups()[0]["queue_max_ms"] == 5


def test_watch_flag(log):
    assert not log.watched()
    log.touch_watch()
    assert log.watched()
    log.c.t += netlog.WATCH_S + 1
    assert not log.watched()
    log.force_watch(60)
    log.c.t += 30
    assert log.watched()
    log.c.t += 31
    assert not log.watched()


def test_sockets_count_and_rate(log):
    a = log.ws_open("fs-events", "shell /")
    b = log.ws_open("fs-events", "shell /")
    c = log.ws_open("run", "app")
    for _ in range(20):
        log.ws_message(a)
    log.ws_close(b)
    rows = {(s["kind"], s["source"]): s for s in log.sockets()}
    assert rows[("fs-events", "shell /")]["open"] == 1
    assert rows[("fs-events", "shell /")]["msgs_per_s"] == 2.0
    assert rows[("run", "app")]["open"] == 1
    assert log.socket_total() == {"fs-events": 1, "run": 1}
    log.ws_close(a)
    log.ws_close(c)
    assert log.sockets() == []
    log.ws_message(a)  # closed token: ignored


def test_stale_open_records_are_reaped(log):
    r = req(log)
    log.c.t += netlog.STALE_OPEN_S + 1
    log.finish(req(log), status=200)  # any finish trims
    rows = {x["id"]: x for x in log.requests()["rows"]}
    assert rows[r["id"]]["outcome"] == "disconnected"
    assert log.summary()["in_flight_total"] == 0


def test_tracked_ws_counts_open_and_frames():
    import asyncio

    class FakeWS:
        query_params = {"page": "/monitor"}
        headers = {}

        def __init__(self):
            self.n = 0

        async def receive(self):
            self.n += 1
            return {"type": "websocket.receive"}

        async def send_text(self, t):
            pass

    seen = {}

    @netlog.tracked_ws("unit-test")
    async def handler(ws):
        seen["open"] = [s for s in netlog.get().sockets() if s["kind"] == "unit-test"]
        await ws.receive()
        await ws.send_text("x")

    asyncio.run(handler(FakeWS()))
    assert seen["open"][0]["open"] == 1 and seen["open"][0]["source"] == "shell /monitor"
    assert [s for s in netlog.get().sockets() if s["kind"] == "unit-test"] == []
