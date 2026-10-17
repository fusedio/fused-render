"""One verdict over the dashboard: ok, or the reasons the app feels slow.

A pure function of the `GET /api/system/dashboard` payload. Rules run in a
fixed order (the likeliest cause of "everything is slow" first); each reads one
section and is skipped when that section is missing or failed. Thresholds are
module constants.
"""
from __future__ import annotations

from urllib.parse import quote

SATURATED_SLOTS = 6
SATURATED_S = 2.0
QUEUE_P95_MS = 500
POOL_BUSY_FRAC = 0.9
LOOP_LAG_MS = 250
SWAP_GROWTH_BYTES = 500 * (1 << 20)
CLAUDE_MAX_COUNT = 8
CLAUDE_MAX_BYTES = 4 * (1 << 30)
MIN_FREE_BYTES = 5 * (1 << 30)
RECENT_OUTAGE_S = 600


def _ok(section) -> dict | None:
    return section if isinstance(section, dict) and "error" not in section else None


def _gb(n: float) -> str:
    return f"{n / (1 << 30):.1f} GB"


def _mb(n: float) -> str:
    return f"{n / (1 << 20):.0f} MB"


def _reason(key: str, text: str, link: str, level: str) -> dict:
    return {"key": key, "text": text, "link": link, "level": level}


def _connections(d: dict):
    net = _ok(d.get("net"))
    summary = _ok((net or {}).get("summary"))
    if not summary:
        return None
    for c in summary.get("in_flight") or []:
        if (c.get("in_flight") or 0) >= SATURATED_SLOTS and (c.get("saturated_s") or 0) > SATURATED_S:
            top = c.get("top") or {}
            who = (f"{top.get('source')} x {top.get('route')}" if top else "unknown requests")
            link = "/monitor?tab=network"
            if top:
                link += f"&source={quote(str(top['source']), safe='')}&route={quote(str(top['route']), safe='')}"
            return _reason("connections", f"All {SATURATED_SLOTS} browser connections held for "
                           f"{c['saturated_s']:.0f}s+ by {who}", link, "bad")
    q = summary.get("browser_queue_p95_ms")
    if q is not None and q > QUEUE_P95_MS:
        return _reason("connections", f"Requests wait {q:.0f} ms in the browser queue (p95)",
                       "/monitor?tab=network", "warn")
    return None


def _threads(d: dict):
    pool = _ok((_ok(d.get("server")) or {}).get("pool"))
    if pool and pool.get("total") and pool["borrowed"] / pool["total"] >= POOL_BUSY_FRAC:
        return _reason("threads", f"Server thread pool {pool['borrowed']}/{pool['total']} busy",
                       "/monitor?tab=network", "bad")
    return None


def _loop(d: dict):
    lag = _ok((_ok(d.get("server")) or {}).get("loop_lag"))
    cur = (lag or {}).get("current_ms")
    if cur is not None and cur > LOOP_LAG_MS:
        return _reason("loop", f"Event loop lagging {cur:.0f} ms", "/monitor?tab=overview", "bad")
    return None


def _memory(d: dict):
    mem = _ok(d.get("memory"))
    if not mem:
        return None
    pressure = mem.get("pressure")
    growth = mem.get("swap_growth_10m_bytes") or 0
    if pressure not in ("warn", "critical") and growth <= SWAP_GROWTH_BYTES:
        return None
    parts = []
    if pressure in ("warn", "critical"):
        parts.append(f"memory pressure {pressure}")
    if growth > SWAP_GROWTH_BYTES:
        parts.append(f"swap grew {_mb(growth)} in 10 min")
    big = mem.get("biggest") or {}
    link = "/monitor?tab=processes"
    if big:
        parts.append(f"biggest: {big.get('label')} ({_mb(big.get('mem') or 0)})")
        link += f"&pid={big.get('pid')}"
    text = "; ".join(parts)
    return _reason("memory", text[0].upper() + text[1:], link,
                   "bad" if pressure == "critical" else "warn")


def _claude(d: dict):
    c = _ok(d.get("claude"))
    if c and ((c.get("total") or 0) > CLAUDE_MAX_COUNT or (c.get("mem_bytes") or 0) > CLAUDE_MAX_BYTES):
        return _reason("claude", f"{c.get('total', 0)} Claude CLIs open using {_gb(c.get('mem_bytes') or 0)}",
                       "/monitor?tab=processes", "warn")
    return None


def _index(d: dict):
    i = _ok(d.get("index"))
    if i and ((i.get("waiting") or 0) > 0 or (i.get("abandoned") or 0) > 0):
        return _reason("index", f"Index lanes: {i.get('waiting', 0)} waiting, "
                       f"{i.get('abandoned', 0)} abandoned reads", "/monitor?tab=overview", "warn")
    return None


def _thermal(d: dict):
    c = _ok(d.get("cpu"))
    if not c:
        return None
    limit = c.get("thermal_limit")
    if limit is not None and limit < 100:
        return _reason("thermal", f"CPU throttled to {limit}% by the OS", "/monitor?tab=overview", "warn")
    if c.get("low_power"):
        return _reason("thermal", "Low Power Mode is on", "/monitor?tab=overview", "warn")
    return None


def _disk(d: dict):
    free = (_ok(d.get("disk")) or {}).get("free_bytes")
    if free is not None and free < MIN_FREE_BYTES:
        return _reason("disk", f"Only {_gb(free)} of disk free", "/monitor?tab=overview", "warn")
    return None


def _outage(d: dict):
    age = (_ok(d.get("errors")) or {}).get("last_outage_age_s")
    if age is not None and age < RECENT_OUTAGE_S:
        return _reason("outage", f"Server outage {age / 60:.0f} min ago", "/monitor?tab=overview", "bad")
    return None


RULES = (_connections, _threads, _loop, _memory, _claude, _index, _thermal, _disk, _outage)


def verdict(dashboard: dict) -> dict:
    reasons = []
    for rule in RULES:
        try:
            r = rule(dashboard or {})
        except Exception:  # noqa: BLE001 — a malformed section skips its rule
            r = None
        if r:
            reasons.append(r)
    if not reasons:
        return {"level": "ok", "headline": "Nothing looks wrong", "reasons": []}
    level = "bad" if any(r["level"] == "bad" for r in reasons) else "warn"
    return {"level": level, "headline": reasons[0]["text"], "reasons": reasons}
