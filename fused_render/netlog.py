"""In-memory log of the server's own HTTP requests and WebSockets, and the
rollups the Monitor page's Network tab and the verdict are built from.

WHY: "fused-render is slow" has a cause the server could not see. WebKit allows
6 HTTP/1.1 connections per host:port, shared by EVERY WKWebView window of the
app, so a page spamming requests queues its OWN and every other window's
requests inside the browser. The server only ever saw each request start when
it finally got a connection. This module records what the server sees (who asked,
what, how long it was held open) and merges what each document measured about
its own queueing (`client_report`, fed by the beacon in platform/lib/netTiming.ts
and static/runtime.js, only while someone is watching).

Stdlib only, thread-safe, nothing persisted: a ring of the last RETAIN_S seconds
(hard-capped at MAX_RECORDS) plus the set of requests still open. The rollups
(`summary`, `groups`, `requests`) are thin methods over module-level pure
functions that take the records and a clock, so they are unit-tested without a
server.
"""
from __future__ import annotations

import collections
import functools
import itertools
import os
import re
import threading
import time
from urllib.parse import parse_qsl, unquote, urlsplit

RETAIN_S = 300.0
MAX_RECORDS = 20000
# "Watched" = something polled a dashboard/chip endpoint in the last WATCH_S.
# Mirrors sysmon/sampler.py IDLE_AFTER_S: while nobody looks, nothing extra is
# measured, sent or computed.
WATCH_S = 15.0
LONG_HELD_S = 5.0
# A response whose client left before its body started never runs its cleanup;
# an open record that old is closed rather than leaked.
STALE_OPEN_S = 900.0
SATURATION_SLOTS = 6
SOCKET_WINDOW_S = 10.0
CLIENT_STATS_S = 300.0
# Distinct (source, route) keys the beacon may hold: it is unauthenticated, so
# a hostile poster could otherwise mint keys without bound.
MAX_CLIENT_KEYS = 512
MAX_REQUEST_ROWS = 1000
# The beacon is excluded from the ring entirely; every other dashboard poll is
# recorded but flagged `self` so the UI can hide it.
BEACON_PATH = "/api/system/net/client"
_SELF_PREFIXES = ("/api/system/",)

_ID_HEX = re.compile(r"^[0-9a-fA-F]{8,}$")
_ID_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ID_NUM = re.compile(r"^\d+$")


# ---------------------------------------------------------------- pure helpers

def _is_id_segment(seg: str) -> bool:
    return bool(seg) and (len(seg) > 40 or _ID_NUM.match(seg) is not None
                          or _ID_UUID.match(seg) is not None
                          or _ID_HEX.match(seg) is not None)


def normalize_route(path: str) -> str:
    """`path` without its query string, id-looking segments collapsed to
    `{id}` (all-digit, hex of 8+, uuid, or longer than 40 chars). The same rule
    lives in frontend/src/platform/lib/netTiming.ts and static/runtime.js so
    the client's queue stats join the server's groups on one key."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    return "/".join("{id}" if _is_id_segment(s) else s for s in path.split("/"))


def full_path(path: str, query: str) -> str:
    """The request path with the query's VALUES stripped, keys kept."""
    keys = [k for k, _ in parse_qsl(query or "", keep_blank_values=True)]
    return path + ("?" + "&".join(keys) if keys else "")


def ua_family(ua: str) -> str:
    u = (ua or "").lower()
    if not u:
        return "other"
    if u.startswith("curl") or "python-" in u or "httpx" in u:
        return "curl"
    if "firefox" in u:
        return "Firefox"
    if "edg/" in u or "chrome" in u or "chromium" in u:
        return "Chrome"
    if "applewebkit" in u and "safari" not in u:
        return "WebKit app"
    if "safari" in u:
        return "Safari"
    return "other"


def client_key(addr: str, ua: str) -> str:
    return f"{addr or '?'} {ua_family(ua)}"


def _app_name(page: str) -> str:
    """The page file's folder name. Pure string work: this runs on the event
    loop (middleware, websocket wrapper), so no stat, no project lookup."""
    folder = os.path.dirname(page.rstrip("/\\"))
    return os.path.basename(folder.rstrip("/\\")) or os.path.basename(page) or page


def source_name(page: str | None, referer: str | None) -> str:
    """Display name of who raised a request. `page` is the decoded
    X-Fused-Page value; without it the Referer's path (or its `path` param,
    which is how the shell addresses a file) stands in. An app file names its
    app folder, a shell route reads `shell /monitor`."""
    if not page and referer:
        try:
            parts = urlsplit(referer)
            q = dict(parse_qsl(parts.query))
            page = q.get("path") or parts.path
        except ValueError:
            page = ""
    if not page:
        return "other"
    page = unquote(page)
    if os.path.isabs(page) and not page.startswith("//"):
        # `/monitor` is a shell route AND an absolute path: a route has no
        # extension and no further directory, a page file does.
        if os.path.splitext(page)[1] or page.count("/") > 1:
            return _app_name(page)
    route = page if page.startswith("/") else "/" + page
    return "shell " + route.split("?", 1)[0]


def percentile(values, pct: float):
    vs = sorted(values)
    if not vs:
        return None
    k = (len(vs) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 1)


def _is_error(rec: dict) -> bool:
    return rec.get("outcome") == "error" or (rec.get("status") or 0) >= 500


def _status_class(rec: dict) -> str:
    s = rec.get("status")
    return f"{s // 100}xx" if s else ("open" if rec.get("outcome") is None else "none")


def concurrency(finished, open_recs, now: float, window_s: float = 60.0,
                step_s: float = 1.0) -> dict[str, dict]:
    """Per-client simultaneous-request stats over the last `window_s`, computed
    from request intervals (not snapshots, which miss millisecond requests).
    Returns {client: {peak, series, at_limit_s}}; `series` has window_s/step_s
    buckets, oldest first, each the max overlap seen inside that bucket."""
    t0 = now - window_s
    nbuckets = max(1, int(round(window_s / step_s)))
    per: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for r in finished:
        if r.get("self") or r.get("end_t") is None:
            continue
        if r["end_t"] >= t0:
            per[r["client"]].append((r["start_mono"], r["end_t"]))
    for r in open_recs:
        if not r.get("self"):
            per[r["client"]].append((r["start_mono"], now))
    out: dict[str, dict] = {}
    for client, ivs in per.items():
        events = []
        for a, b in ivs:
            a, b = max(a, t0), min(max(b, a), now)
            events.append((a, 1))
            events.append((b, -1))
        events.sort(key=lambda e: (e[0], e[1]))  # ends before starts at a tie
        series = [0] * nbuckets
        cur = peak = 0
        at_limit = 0.0
        last = t0

        def mark(lo: float, hi: float, level: int) -> None:
            # Level holds on [lo, hi]; credit every bucket it touches.
            if level <= 0:
                return
            i0 = min(nbuckets - 1, max(0, int((lo - t0) / step_s)))
            i1 = min(nbuckets - 1, max(0, int((hi - t0) / step_s)))
            for i in range(i0, i1 + 1):
                if series[i] < level:
                    series[i] = level

        for t, d in events:
            if cur >= SATURATION_SLOTS:
                at_limit += t - last
            mark(last, t, cur)
            cur += d
            peak = max(peak, cur)
            mark(t, t, cur)
            last = t
        if cur >= SATURATION_SLOTS:
            at_limit += now - last
        out[client] = {"peak": peak, "series": series, "at_limit_s": round(at_limit, 1)}
    return out


def summarize(finished, open_recs, now: float, queue_samples=()) -> dict:
    """req/s over 10 s and 60 s, per-client in-flight and saturation, errors,
    and the long-held list. `finished` are records newest-last; `queue_samples`
    are browser queue_ms readings from the last 60 s."""
    recent = [r for r in finished if now - r["end_t"] <= 60.0] if finished else []
    n10 = sum(1 for r in recent if now - r["end_t"] <= 10.0)
    per_client: dict[str, list[dict]] = collections.defaultdict(list)
    for r in open_recs:
        if not r.get("self"):
            per_client[r["client"]].append(r)
    clients = []
    for key, rows in per_client.items():
        ages = sorted((now - r["start_mono"] for r in rows))  # youngest first
        sat = ages[SATURATION_SLOTS - 1] if len(ages) >= SATURATION_SLOTS else 0.0
        holders = collections.Counter((r["source"], r["route"]) for r in rows)
        top = holders.most_common(1)
        clients.append({
            "client": key, "in_flight": len(rows),
            "saturated_s": round(sat, 2),
            "top": ({"source": top[0][0][0], "route": top[0][0][1], "count": top[0][1]}
                    if top else None),
        })
    clients.sort(key=lambda c: -c["in_flight"])
    opens = [r for r in open_recs if not r.get("self")]
    oldest = max((now - r["start_mono"] for r in opens), default=0.0)
    long_held = []
    for r in opens:
        age = now - r["start_mono"]
        if age > LONG_HELD_S:
            long_held.append(_row(r, now))
    for r in finished:
        if (r.get("held_ms") or 0) > LONG_HELD_S * 1000 and not r.get("self"):
            long_held.append(_row(r, now))
    long_held.sort(key=lambda r: -(r.get("held_ms") or 0))
    qp95 = percentile(queue_samples, 95) if queue_samples else None
    conc = [{"client": k, **v} for k, v in concurrency(finished, open_recs, now).items()]
    conc.sort(key=lambda c: -c["peak"])
    return {
        "concurrency": conc,
        "main_client": conc[0]["client"] if conc else None,
        "rps_10s": round(n10 / 10.0, 2),
        "rps_60s": round(len(recent) / 60.0, 2),
        "in_flight": clients,
        "in_flight_total": len(opens),
        "oldest_in_flight_s": round(oldest, 2),
        "errors_60s": sum(1 for r in recent if _is_error(r) and not r.get("self")),
        "long_held": long_held[:50],
        "browser_queue_p95_ms": qp95,
    }


def _row(r: dict, now: float | None = None) -> dict:
    held = r.get("held_ms")
    if held is None and now is not None:
        held = round((now - r["start_mono"]) * 1000, 1)
    return {
        "id": r["id"], "seq": r["seq"], "t": r["t"], "method": r["method"],
        "route": r["route"], "path": r["path"], "status": r["status"],
        "server_ms": r["server_ms"], "held_ms": held, "bytes": r["bytes"],
        "source": r["source"], "client": r["client"], "outcome": r["outcome"],
        "self": r["self"],
    }


def group_rows(records, now: float, window_s: float, client_stats: dict) -> list[dict]:
    """source x route over the last `window_s`: count, per-minute rate,
    p50/p95 server_ms, errors, max concurrent same-group requests, and the
    merged browser queue stats (`client_stats[(source, route)]`)."""
    by: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in records:
        end = r.get("end_t") or now
        if now - end <= window_s and not r.get("self"):
            by[(r["source"], r["route"])].append(r)
    out = []
    for (source, route), rs in by.items():
        ms = [r["server_ms"] for r in rs if r["server_ms"] is not None
              and r.get("outcome") != "disconnected"]
        cs = client_stats.get((source, route), {})
        out.append({
            "source": source, "route": route, "count": len(rs),
            "per_min": round(len(rs) * 60.0 / window_s, 1),
            "p50_ms": percentile(ms, 50), "p95_ms": percentile(ms, 95),
            "errors": sum(1 for r in rs if _is_error(r)),
            "max_in_flight": max((r.get("conc") or 1) for r in rs),
            "queue_p95_ms": cs.get("p95"), "queue_max_ms": cs.get("max"),
        })
    out.sort(key=lambda g: (-g["per_min"], g["source"], g["route"]))
    return out


def filter_rows(rows, *, source=None, route=None, status=None, min_ms=None,
                open_only=False, include_self=False):
    out = []
    for r in rows:
        if r["self"] and not include_self:
            continue
        if source and r["source"] != source:
            continue
        if route and r["route"] != route:
            continue
        if status and _status_class(r) != status:
            continue
        if open_only and r["outcome"] is not None:
            continue
        if min_ms is not None and (r["held_ms"] or 0) < min_ms:
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------- the log

class NetLog:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._seq = 0
        self._finished: collections.deque = collections.deque()
        self._open: dict[int, dict] = {}
        self._open_by_group: dict[tuple, int] = collections.defaultdict(int)
        # (source, route) -> deque of (mono t, count, sum, max, [samples])
        self._client: dict[tuple, collections.deque] = {}
        self._sockets: dict[int, dict] = {}
        self._socket_ids = itertools.count(1)
        self._socket_msgs: dict[tuple, collections.deque] = {}
        self._last_watch = float("-inf")
        self._forced_until = float("-inf")

    # -- watching ------------------------------------------------------------
    def touch_watch(self) -> None:
        self._last_watch = self._clock()

    def force_watch(self, seconds: float) -> None:
        self._forced_until = max(self._forced_until, self._clock() + seconds)

    def watched(self) -> bool:
        now = self._clock()
        return now - self._last_watch <= WATCH_S or now <= self._forced_until

    # -- requests ------------------------------------------------------------
    def begin(self, method: str, path: str, query: str, headers, client_addr: str) -> dict | None:
        if path == BEACON_PATH:
            return None
        source = source_name(_decode(headers.get("x-fused-page")), headers.get("referer"))
        route = normalize_route(path)
        rec = {
            "id": 0, "seq": 0, "t": self._wall(), "start_mono": self._clock(),
            "method": method, "route": route, "path": full_path(path, query),
            "status": None, "server_ms": None, "held_ms": None, "bytes": None,
            "source": source, "client": client_key(client_addr, headers.get("user-agent", "")),
            "outcome": None, "self": path.startswith(_SELF_PREFIXES), "end_t": None,
        }
        group = (source, route)
        with self._lock:
            rec["id"] = next(self._ids)
            self._seq += 1
            rec["seq"] = self._seq
            self._open_by_group[group] += 1
            rec["conc"] = self._open_by_group[group]
            self._open[rec["id"]] = rec
        return rec

    def headers_sent(self, rec: dict | None, status: int, server_ms: float) -> None:
        if rec is None:
            return
        with self._lock:
            rec["status"] = status
            rec["server_ms"] = round(server_ms, 1)
            self._seq += 1
            rec["seq"] = self._seq

    def finish(self, rec: dict | None, *, status: int | None = None,
               server_ms: float | None = None, nbytes: int | None = None,
               outcome: str = "ok") -> None:
        if rec is None:
            return
        with self._lock:
            self._close_locked(rec, self._clock(), status, server_ms, nbytes, outcome)
            self._trim(self._clock())

    def _close_locked(self, rec, now, status, server_ms, nbytes, outcome) -> None:
        if rec["id"] not in self._open:
            return  # finished twice (body end + a late cancel)
        del self._open[rec["id"]]
        group = (rec["source"], rec["route"])
        self._open_by_group[group] -= 1
        if self._open_by_group[group] <= 0:
            del self._open_by_group[group]
        if status is not None:
            rec["status"] = status
        if server_ms is not None:
            rec["server_ms"] = round(server_ms, 1)
        if outcome == "ok" and (rec["status"] or 0) >= 500:
            outcome = "error"
        rec["bytes"] = nbytes
        rec["outcome"] = outcome
        rec["end_t"] = now
        rec["held_ms"] = round((now - rec["start_mono"]) * 1000, 1)
        if rec["server_ms"] is None:
            rec["server_ms"] = rec["held_ms"]
        self._seq += 1
        rec["seq"] = self._seq
        self._finished.append(rec)

    def _trim(self, now: float) -> None:
        for rid in [i for i, r in self._open.items() if now - r["start_mono"] > STALE_OPEN_S]:
            self._close_locked(self._open[rid], now, None, None, None, "disconnected")
        f = self._finished
        while f and (len(f) > MAX_RECORDS or now - f[0]["end_t"] > RETAIN_S):
            f.popleft()

    def _snapshot(self):
        with self._lock:
            self._trim(self._clock())
            return list(self._finished), list(self._open.values())

    # -- browser wait stats --------------------------------------------------
    def client_report(self, source: str, routes: dict) -> None:
        """Merge one document's per-route queue stats: `{route: {count, sum,
        max, samples}}`. Bounded: routes per report, samples per route."""
        now = self._clock()
        with self._lock:
            # Expired entries go on every report, not only when a rollup reads
            # them, so a beacon nobody is watching still cannot grow the map.
            for key in list(self._client):
                dq = self._client[key]
                while dq and now - dq[0][0] > CLIENT_STATS_S:
                    dq.popleft()
                if not dq:
                    del self._client[key]
            for route, st in list(routes.items())[:200]:
                try:
                    entry = (now, int(st.get("count", 0)), float(st.get("sum", 0)),
                             float(st.get("max", 0)),
                             [float(x) for x in (st.get("samples") or [])[:64]])
                except (TypeError, ValueError, AttributeError):
                    continue
                key = (source, normalize_route(str(route)))
                if key not in self._client:
                    if len(self._client) >= MAX_CLIENT_KEYS:
                        # Evict the key that was updated longest ago.
                        oldest = min(self._client, key=lambda k: self._client[k][-1][0])
                        del self._client[oldest]
                    self._client[key] = collections.deque(maxlen=200)
                self._client[key].append(entry)

    def _client_stats(self, window_s: float = CLIENT_STATS_S) -> dict:
        now = self._clock()
        out = {}
        with self._lock:
            for key in list(self._client):
                dq = self._client[key]
                while dq and now - dq[0][0] > CLIENT_STATS_S:
                    dq.popleft()
                if not dq:
                    del self._client[key]
                    continue
                recent = [e for e in dq if now - e[0] <= window_s]
                if not recent:
                    continue
                samples = [s for e in recent for s in e[4]]
                out[key] = {"count": sum(e[1] for e in recent),
                            "max": max(e[3] for e in recent),
                            "p95": percentile(samples, 95)}
        return out

    def _queue_samples(self, window_s: float) -> list:
        now = self._clock()
        with self._lock:
            return [s for dq in self._client.values() for e in dq
                    if now - e[0] <= window_s for s in e[4]]

    # -- rollups -------------------------------------------------------------
    def summary(self) -> dict:
        finished, opens = self._snapshot()
        return summarize(finished, opens, self._clock(), self._queue_samples(60.0))

    def groups(self, window_s: float = 60.0) -> list[dict]:
        finished, opens = self._snapshot()
        window_s = max(1.0, min(float(window_s), RETAIN_S))
        return group_rows(finished + opens, self._clock(), window_s,
                          self._client_stats(window_s))

    def requests(self, since_id: int = 0, *, limit: int = MAX_REQUEST_ROWS, **filters) -> dict:
        """Rows whose state changed after cursor `since_id` (a `seq`): new
        requests, and ones that opened earlier and have since finished. The
        client merges by `id` and keeps the returned `cursor`."""
        now = self._clock()
        with self._lock:
            self._trim(now)
            changed = []
            for r in reversed(self._finished):
                if r["seq"] <= since_id:
                    break  # the deque is in finish (= seq) order
                changed.append(r)
            changed += [r for r in self._open.values() if r["seq"] > since_id]
            cursor = self._seq
        changed.sort(key=lambda r: r["seq"])
        rows = filter_rows([_row(r, now) for r in changed], **filters)
        if len(rows) > limit:
            rows = rows[-limit:]
        return {"rows": rows, "cursor": cursor}

    # -- websockets ----------------------------------------------------------
    def ws_open(self, kind: str, source: str = "other") -> int:
        with self._lock:
            token = next(self._socket_ids)
            self._sockets[token] = {"kind": kind, "source": source}
            return token

    def ws_message(self, token: int) -> None:
        with self._lock:
            s = self._sockets.get(token)
            if s is None:
                return
            dq = self._socket_msgs.setdefault((s["kind"], s["source"]),
                                              collections.deque(maxlen=5000))
            dq.append(self._clock())

    def ws_close(self, token: int) -> None:
        with self._lock:
            self._sockets.pop(token, None)

    def sockets(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            counts = collections.Counter((s["kind"], s["source"])
                                         for s in self._sockets.values())
            out = []
            for (kind, source), n in counts.items():
                dq = self._socket_msgs.get((kind, source), ())
                msgs = sum(1 for t in dq if now - t <= SOCKET_WINDOW_S)
                out.append({"kind": kind, "source": source, "open": n,
                            "msgs_per_s": round(msgs / SOCKET_WINDOW_S, 2)})
            for key in [k for k, dq in self._socket_msgs.items()
                        if k not in counts and not any(now - t <= SOCKET_WINDOW_S for t in dq)]:
                del self._socket_msgs[key]
        out.sort(key=lambda s: (-s["open"], s["kind"], s["source"]))
        return out

    def socket_total(self) -> dict:
        with self._lock:
            return dict(collections.Counter(s["kind"] for s in self._sockets.values()))


def tracked_ws(kind: str):
    """Decorator for a `@router.websocket` endpoint: count the socket as open
    for as long as the handler runs, and its frames (both directions) toward
    msgs/s. WS scopes bypass the HTTP middleware, so each socket kind opts in
    here. Wraps the connection's send/receive instead of editing every
    handler's loop; any failure in the bookkeeping is swallowed."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ws = next((a for a in list(args) + list(kwargs.values())
                       if hasattr(a, "receive") and hasattr(a, "query_params")), None)
            token = None
            try:
                token = _log.ws_open(kind, _ws_source(ws)) if ws is not None else None
                for name in ("receive", "send_text", "send_bytes"):
                    _count_calls(ws, name, token)
            except Exception:  # noqa: BLE001 — keep the token: it must still close
                pass
            try:
                return await fn(*args, **kwargs)
            finally:
                if token is not None:
                    _log.ws_close(token)
        return wrapper
    return deco


def _count_calls(ws, name: str, token) -> None:
    inner = getattr(ws, name, None)
    if inner is None:
        return

    async def counted(*a, **kw):
        result = await inner(*a, **kw)
        if token is not None:
            _log.ws_message(token)
        return result

    setattr(ws, name, counted)


def _ws_source(ws) -> str:
    try:
        page = ws.query_params.get("page") or ws.query_params.get("path")
        return source_name(page, ws.headers.get("referer"))
    except Exception:  # noqa: BLE001
        return "other"


def _decode(value: str | None) -> str | None:
    return unquote(value) if value else None


_log = NetLog()


def get() -> NetLog:
    return _log
