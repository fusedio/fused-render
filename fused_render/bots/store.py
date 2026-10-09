"""The bots' on-disk store (docs/bots.md §1, §2): bot dirs, bot.json,
events.jsonl, the usage ledger and its summary, the transcript export, and
the Builds panel's list. Ported from OpenBot agents.py (`_bot_dir`,
`_read_meta`, `_list_ids`, `_iter_events`, `_usage_log`, `_usage_summary`,
`_export_md`) and browser.py (`write_json_atomic`).

Every path resolves through `fused_render.bots.paths` on each call, so
FUSED_RENDER_HOME can be redirected after import (tests do).
"""
from __future__ import annotations

import json
import os
import threading
import time

from fused_render.bots import paths as _bpaths

BOT_FILE = "bot.json"
EVENTS_FILE = "events.jsonl"
USAGE_KEEP_DAYS = 7
USAGE_CACHE_S = 10
USAGE_PRUNE_AT = 500  # stale lines before the ledger is rewritten without them


# ------------------------------------------------------------------ files ---

def _publish(topic: str, key=None) -> None:
    """Wake the events bus (server/events.py) for `topic`. Imported lazily so
    this module keeps importing in a process that never starts the server."""
    try:
        from fused_render.server.events import bus
        bus.publish(topic, key)
    except Exception:  # noqa: BLE001 — a missed wake is latency, never an error
        pass

def write_json_atomic(path: str, obj) -> None:
    """Write to a temp file beside `path` and rename it into place. The temp name
    carries pid + thread id: a shared "<path>.tmp" let two writers (a bot's task
    thread and the poll thread both saving) race, and the loser's os.replace
    failed with FileNotFoundError after the winner had renamed it."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        # On Windows, replacing a destination another thread is renaming onto
        # at the same instant can raise a transient PermissionError (a sharing
        # violation) even though the two never touch each other's own temp
        # file — POSIX rename() has no such restriction. Retried briefly
        # rather than failing a write that only lost a benign race.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------- bots ---
def bot_dir(bid: str) -> str:
    return _bpaths.bot_dir(bid)


def meta_path(bid: str) -> str:
    return os.path.join(bot_dir(bid), BOT_FILE)


def events_path(bid: str) -> str:
    return os.path.join(bot_dir(bid), EVENTS_FILE)


def read_meta(bid: str) -> dict:
    """bot.json; raises OSError / ValueError like OpenBot `_read_meta`."""
    with open(meta_path(bid)) as f:
        return json.load(f)


def write_meta(bid: str, meta: dict) -> None:
    os.makedirs(bot_dir(bid), exist_ok=True)
    write_json_atomic(meta_path(bid), meta)


def list_ids() -> list[str]:
    """Every bot id with a bot.json, sorted."""
    data = _bpaths.data_dir()
    try:
        names = os.listdir(data)
    except OSError:
        return []
    return sorted(d for d in names if os.path.isfile(os.path.join(data, d, BOT_FILE)))


# ----------------------------------------------------------------- events ---
def iter_events(path: str):
    """(line_index, event) for every parseable line of an events.jsonl.
    The index counts physical lines, torn or not, so page cursors (which are
    line counts) never drift after a bad write. Missing file -> nothing."""
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                try:
                    yield i, json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def count_events(path: str) -> int:
    """Physical lines in the file (OpenBot `Bot._count_events`): the seq to start from."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def append_event(path: str, ev: dict) -> dict:
    """One line onto events.jsonl. The caller holds the bot's lock (and owns `seq`)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def events_since(path: str, cursor: int) -> list[dict]:
    """Events at line index >= `cursor`, by position in the file. The cursor
    is a line count (the `seq` the page last saw), not a seq filter: anything
    appended by another writer with a lower seq still reaches the page. The
    caller keeps its seq counter ahead (`max(seq, cursor + len(out))`)."""
    try:
        cursor = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        cursor = 0
    return [ev for i, ev in iter_events(path) if i >= cursor]


# ------------------------------------------------------------------ usage ---
# One line per model call. The file is the shared truth; the summary the page
# shows is recomputed at most every USAGE_CACHE_S.
_usage_lock = threading.Lock()
_usage_cache: tuple[float, dict | None] = (0.0, None)


def usage_log(bid, model, origin, task, ok, name=None, cost=None, input_tokens=None) -> None:
    """Append one call. The name travels with the line so a bot deleted later
    still shows up by name, not by id. `cost` (USD) and `input_tokens` are
    written only when known (the agent engine reports them; OpenBot did not)."""
    global _usage_cache
    ev = {"ts": time.time(), "bot": bid, "name": name or "", "model": model, "origin": origin,
          "task": (task or "")[:80], "ok": bool(ok)}
    if cost is not None:
        ev["cost"] = cost
    if input_tokens is not None:
        ev["input_tokens"] = input_tokens
    path = _bpaths.usage_path()
    with _usage_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(ev) + "\n")
        _usage_cache = (0.0, None)


def usage_reset_cache() -> None:
    global _usage_cache
    with _usage_lock:
        _usage_cache = (0.0, None)


def usage_summary(now: float | None = None) -> dict:
    """Calls today / last hour / per bot / per task / per hour / per day (the
    OpenBot shape): {today, hour, errors, origin: {routine, manual}, bots: [...],
    tasks: {task: n}, hours: [24, oldest -> newest], days: [{day, n}] x 7}.
    `now` is for tests and bypasses the cache."""
    global _usage_cache
    use_cache = now is None
    now = time.time() if now is None else now
    if use_cache:
        ts, cached = _usage_cache
        if cached is not None and now - ts < USAGE_CACHE_S:
            return cached
    path = _bpaths.usage_path()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    keep_from = now - USAGE_KEEP_DAYS * 86400
    rows, stale = [], 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("ts", 0) < keep_from:
                    stale += 1
                    continue
                rows.append(ev)
    except FileNotFoundError:
        pass
    if stale > USAGE_PRUNE_AT:  # prune old lines now and then; the file is append-only otherwise
        with _usage_lock:
            tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                f.writelines(json.dumps(ev) + "\n" for ev in rows)
            os.replace(tmp, path)
    live = set(list_ids())
    names = {}
    for bid in live:
        try:
            names[bid] = read_meta(bid).get("name") or bid
        except Exception:  # noqa: BLE001
            names[bid] = bid
    # Per bot: today, this week, model mix, failures, last call. Bots that no longer exist keep their logged
    # name; lines from before names were logged collapse into one "Deleted bots" row.
    per_bot, per_task, days = {}, {}, {}
    hours = [0] * 24          # index 0 = the current hour, filled backwards
    origin = {"routine": 0, "manual": 0}
    today = hour = errors = 0
    for ev in rows:
        t = ev.get("ts", 0)
        bid = ev.get("bot", "?")
        key = bid if (bid in live or ev.get("name")) else "_deleted"
        pb = per_bot.setdefault(key, {"id": key, "name": "", "today": 0, "week": 0, "models": {}, "errors": 0,
                                      "last": 0, "live": key in live})
        pb["name"] = names.get(bid) or ev.get("name") or pb["name"] or "Deleted bots"
        pb["week"] += 1
        pb["models"][ev.get("model") or "?"] = pb["models"].get(ev.get("model") or "?", 0) + 1
        pb["last"] = max(pb["last"], t)
        if not ev.get("ok", True):
            pb["errors"] += 1
        if t >= midnight:
            today += 1
            pb["today"] += 1
            per_task[ev.get("task", "")] = per_task.get(ev.get("task", ""), 0) + 1
            o = ev.get("origin") if ev.get("origin") in origin else "manual"
            origin[o] += 1
            if not ev.get("ok", True):
                errors += 1
        if t >= now - 3600:
            hour += 1
        if t >= now - 86400:
            hours[min(23, int((now - t) // 3600))] += 1
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        days[day] = days.get(day, 0) + 1
    hours.reverse()           # oldest -> newest, like a timeline
    day_list = [{"day": d, "n": days.get(d, 0)} for d in
                (time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(USAGE_KEEP_DAYS - 1, -1, -1))]
    out = {"today": today, "hour": hour, "errors": errors, "origin": origin,
           "bots": sorted(per_bot.values(), key=lambda b: (-b["today"], -b["week"])),
           "tasks": per_task, "hours": hours, "days": day_list}
    if use_cache:
        _usage_cache = (now, out)
    return out


# ----------------------------------------------------------------- export ---
def export_markdown(name: str, events_path: str) -> str:
    """The whole transcript as Markdown (OpenBot `_export_md`)."""
    out = [f"# {name or 'Bot'} — transcript", ""]
    for _, ev in iter_events(events_path):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
        role, text = ev.get("role"), (ev.get("text") or "").rstrip()
        if role == "user":
            out += [f"**You** · {ts}", "", text, ""]
        elif role == "done":
            out += [f"**Bot finished** · {ts}", "", text, ""]
        elif role in ("question", "approval"):
            out += [f"**Bot asked** · {ts}", "", text, ""]
        elif role == "thought":
            out += [f"_{text}_", ""]
        elif role == "note":  # harness line (e.g. "Used X › tool"), muted
            out += [f"> _{text}_", ""]
        elif role == "action":
            res = (ev.get("result") or "").split("\n")[0]
            out += [f"- `{text}` → {res}"]
        elif role == "error":
            out += [f"> ⚠ {text}", ""]
        elif role == "system":
            out += [f"> {text}", ""]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- builds ---
_builds_lock = threading.Lock()


def builds_read() -> list:
    """The Builds panel's list (`<home>/bots/data/builds.json`); [] when missing or unreadable."""
    try:
        with open(_bpaths.builds_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):  # tolerate {"builds": [...]}
        data = data.get("builds")
    return [b for b in data if isinstance(b, dict)] if isinstance(data, list) else []


def builds_write(builds: list) -> None:
    """Replace the list (the page owns it; POST /api/bots/builds). Raises ValueError when it is not a list."""
    if not isinstance(builds, list):
        raise ValueError("builds must be a list")
    with _builds_lock:
        write_json_atomic(_bpaths.builds_path(), builds)
    _publish("bots.builds")
