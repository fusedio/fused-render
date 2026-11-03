"""The bots' on-disk store (docs/bots.md §1, §2): bot.json + state.json (the
public/private split of a bot's meta), events.jsonl, the usage ledger and its
summary, the transcript export, and the Builds panel's list. Ported from
OpenBot agents.py (`_bot_dir`, `_read_meta`, `_list_ids`, `_iter_events`,
`_usage_log`, `_usage_summary`, `_export_md`) and browser.py
(`write_json_atomic`).

Every path resolves through `fused_render.bots.paths` on each call, so
FUSED_RENDER_HOME / FUSED_RENDER_DIR can be redirected after import (tests do).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid

from fused_render.bots import paths as _bpaths

BOT_FILE = "bot.json"
EVENTS_FILE = "events.jsonl"
USAGE_KEEP_DAYS = 7
USAGE_CACHE_S = 10
USAGE_PRUNE_AT = 500  # stale lines before the ledger is rewritten without them


# ------------------------------------------------------------------ files ---
def write_json_atomic(path: str, obj) -> None:
    """Write to a temp file beside `path` and rename it into place. The temp name
    carries pid + thread id: a shared "<path>.tmp" let two writers (a bot's task
    thread and the poll thread both saving) race, and the loser's os.replace
    failed with FileNotFoundError after the winner had renamed it."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------- bots ---
# The bot.json split (bots v3): a bot folder is meant to be shared (exported, committed to git), so its root
# bot.json carries only what DEFINES the bot; everything the runtime writes (status, counters, browser id,
# phone handle, routine run state, …) goes to `.fused/state.json`, which is gitignored. The in-memory Bot.meta
# stays ONE dict: read_meta merges, write_meta splits, and nothing outside this module knows there are two files.
PUBLIC_KEYS = frozenset({"id", "name", "model", "effort", "instructions", "face", "preset", "kind", "approval",
                         "build_access", "terminal", "engine", "max_steps", "step_cap", "super_access", "created",
                         "apps", "routines"})
# A routine row is split the same way, by its `id`: the definition is public; last, last_result, last_message,
# last_done, next, fails, runs, … are run state and private.
# `enabled` is NOT public: the runtime flips it (a once-routine completing, the failure circuit breaker), and
# a public file that churned on every such event would defeat the git-clean rule; an imported routine starts
# disabled anyway (botfile).
ROUTINE_PUBLIC_KEYS = frozenset({"id", "task", "kind", "created", "minutes", "anchor", "time", "weekdays", "at"})
STATE_FILE = "state.json"
# The two halves are two atomic replaces, not one: a crash between them would pair a NEW routine schedule with the
# OLD `next` (or the reverse), and the routine would fire once at a time nobody committed. So bot.json carries
# GEN_KEY, minted afresh only when its PUBLIC text changes (a private-only save must not touch the git-facing file),
# and state.json carries PUBLIC_GEN_KEY = the gen of the bot.json it was saved beside. read_meta calls a pair whose
# gens differ torn and drops the schedule-derived run state (ROUTINE_TORN_KEYS) so the scheduler recomputes it.
# Neither key ever reaches the merged meta (read_meta pops both) nor an export (public_json strips GEN_KEY).
GEN_KEY = "_gen"
PUBLIC_GEN_KEY = "_public_gen"
ROUTINE_TORN_KEYS = ("next",)
# The gen pair cannot tell WHICH routine a torn save changed, and dropping `last` on every torn load would re-fire
# finished one-shots. So each private routine row also carries SCHED_REV_KEY, a hash of the public schedule fields
# (ROUTINE_SCHED_KEYS) it was saved beside: a row whose rev no longer matches its bot.json twin (a torn save, a git
# pull, a hand edit that changed the schedule) drops `next` AND `last` (ROUTINE_RESCHED_KEYS), since an old `last`
# would end a routine flipped to `once` for good. A row without a rev (pre-upgrade) keeps the gen rule alone.
# The rev is private: split_meta routes it to state.json, _merge pops it, so it never reaches the meta or a package.
ROUTINE_SCHED_KEYS = ROUTINE_PUBLIC_KEYS - {"id", "task", "created"}
SCHED_REV_KEY = "sched_rev"
ROUTINE_RESCHED_KEYS = ("next", "last")
_torn_warned: set = set()


def _sched_rev(row: dict) -> str:
    """Short hash of a routine row's public schedule fields (ROUTINE_SCHED_KEYS); see SCHED_REV_KEY."""
    sched = {k: row[k] for k in sorted(ROUTINE_SCHED_KEYS) if k in row}
    text = json.dumps(sched, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def bot_dir(bid: str) -> str:
    """The bot's PRIVATE folder (`<folder>/.fused`); alias of paths.bot_dir, kept for old callers."""
    return _bpaths.bot_dir(bid)


def meta_path(bid: str) -> str:
    """`<folder>/bot.json`, the public half. Raises LookupError for a bid no folder carries."""
    return os.path.join(_bpaths.bot_folder(bid), BOT_FILE)


def state_path(bid: str) -> str:
    """`<folder>/.fused/state.json`, the private half. Raises LookupError for a bid no folder carries."""
    return os.path.join(_bpaths.bot_private(bid), STATE_FILE)


def events_path(bid: str) -> str:
    return os.path.join(_bpaths.bot_private(bid), EVENTS_FILE)


def read_meta(bid: str) -> dict:
    """The bot's whole meta: bot.json overlaid on state.json (public keys win), routines merged by `id`
    (a public row plus its private run state; a private row whose routine is gone is dropped). Raises
    FileNotFoundError when there is no bot.json — also for a bid no folder carries, since every caller treats
    "unknown bot" and "no bot.json" alike (OpenBot `_read_meta` raised OSError for both) — and ValueError on
    a corrupt bot.json. An unreadable or corrupt state.json is moved aside to `state.json.corrupt-<ms>` and the
    bot loads with empty private state (the next save must not overwrite the original); when it cannot be moved
    aside, the read error is raised and the bot does not load."""
    # The folder is resolved ONCE: a second lookup could miss (delete() racing this read) and raise LookupError
    # out of a function whose callers catch OSError; and a read must not create `.fused/` (bot_private would).
    try:
        folder = _bpaths.bot_folder(bid)
    except LookupError as e:
        raise FileNotFoundError(str(e)) from e
    path = os.path.join(folder, BOT_FILE)
    with open(path, encoding="utf-8") as f:
        public = json.load(f)
    if not isinstance(public, dict):
        raise ValueError(f"{path}: not a JSON object")
    spath = os.path.join(folder, _bpaths._PRIVATE, STATE_FILE)
    try:
        with open(spath, encoding="utf-8") as f:
            private = json.load(f)
        if not isinstance(private, dict):
            raise ValueError(f"{spath}: not a JSON object")
    except FileNotFoundError:
        private = {}
    except (OSError, ValueError) as e:
        # The private half is runtime state the bot can rebuild; a torn state.json must not make the bot
        # unloadable when its bot.json is fine. But the bot's next save would overwrite it, so it is moved aside
        # first (never optional): when that fails too, the bot does not load and the original stays untouched.
        aside = f"{spath}.corrupt-{int(time.time() * 1000)}"
        try:
            os.replace(spath, aside)
        except OSError:
            logging.getLogger(__name__).warning("bots: %s unreadable (%s) and cannot be moved aside", spath, e,
                                                exc_info=True)
            raise e from None
        logging.getLogger(__name__).warning("bots: %s unreadable (%s); moved to %s, starting the bot with empty "
                                            "state", spath, e, os.path.basename(aside))
        private = {}
    return _merge(public, private, spath)


def _merge(public: dict, private: dict, where: str, warn: bool = True) -> dict:
    """bot.json overlaid on state.json, routines joined by `id`, the gen stamps checked and removed (GEN_KEY).
    Takes ownership of both dicts (callers pass freshly parsed ones). `warn` off for read-only observers, which
    can land between a live save's two replaces (not a crash)."""
    gen = public.pop(GEN_KEY, None)
    torn = PUBLIC_GEN_KEY in private and private.pop(PUBLIC_GEN_KEY) != gen
    if torn and warn:
        key = (where, gen)
        if key not in _torn_warned:
            _torn_warned.add(key)
            logging.getLogger(__name__).warning("bots: %s was saved beside a different bot.json (a save torn between "
                                                "the two files); routine run times are recomputed", where)
    priv_routines = private.pop("routines", None)
    out = {**private, **public}
    if isinstance(public.get("routines"), list):
        by_id = {r.get("id"): r for r in priv_routines or [] if isinstance(r, dict) and r.get("id")}
        merged = []
        for row in public["routines"]:
            if isinstance(row, dict) and row.get("id") in by_id:
                priv = by_id[row["id"]]
                rev = priv.get(SCHED_REV_KEY)
                drop = set(ROUTINE_TORN_KEYS) if torn else set()
                if rev is not None and rev != _sched_rev(row):
                    drop.update(ROUTINE_RESCHED_KEYS)  # the schedule changed under this run state
                drop.add(SCHED_REV_KEY)
                row = {**row, **{k: v for k, v in priv.items() if k not in ROUTINE_PUBLIC_KEYS and k not in drop}}
            merged.append(row)
        out["routines"] = merged
    return out


def read_all_metas() -> list[dict] | None:
    """Every bot folder's merged meta, read straight off `<bots_root>/*/` (NOT through the folder index, which
    leaves out a folder whose bot.json is torn, foreign-id'd or claimed twice), or None when ANY folder holding a
    bot.json cannot be read whole: a bot.json or .fused/state.json that is a symlink, not a regular file, not
    UTF-8 JSON or not an object, or a `.fused` that is a symlink. For callers that delete things no bot names
    (browsers.sweep): a partial list would read as "nobody uses this". Read-only (a corrupt state.json is NOT
    moved aside, unlike read_meta). Each row's `id` is its bot.json id when that is a string, else the folder
    name. A missing bots root is no bots; an unlistable one is None."""
    root = _bpaths.bots_root()
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    out = []
    for e in entries:
        if e.name.startswith("."):
            continue  # .unmigrated/ and other dot folders are not bot folders (browsers reads .unmigrated itself)
        meta = os.path.join(e.path, BOT_FILE)
        if not os.path.lexists(meta):
            continue  # not a bot folder (or placed, bot.json not written yet: it names no browser)
        try:
            if not e.is_dir(follow_symlinks=False):
                return None  # a symlinked "folder" that holds a bot.json: whose bot it is cannot be trusted
        except OSError:
            return None
        public = _read_object(meta)
        priv_dir = os.path.join(e.path, _bpaths._PRIVATE)
        if public is None or os.path.islink(priv_dir):
            return None
        spath = os.path.join(priv_dir, STATE_FILE)
        private = _read_object(spath) if os.path.lexists(spath) else {}
        if private is None:
            return None
        m = _merge(public, private, spath, warn=False)
        out.append({**m, "id": m["id"] if isinstance(m.get("id"), str) and m["id"] else e.name})
    return out


def _read_object(path: str) -> dict | None:
    """The JSON object in a regular, non-symlink file, or None."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def split_meta(meta: dict) -> tuple[dict, dict]:
    """(public, private) halves of a meta dict, as NEW dicts (Bot.meta is live; never mutate it). Public to
    package.py, which ships exactly the public half."""
    public = {k: v for k, v in meta.items() if k in PUBLIC_KEYS}
    private = {k: v for k, v in meta.items() if k not in PUBLIC_KEYS and k not in (GEN_KEY, PUBLIC_GEN_KEY)}
    routines = meta.get("routines")
    if isinstance(routines, list):
        pub_rows, priv_rows = [], []
        for row in routines:
            if not isinstance(row, dict):
                pub_rows.append(row)
                continue
            if not row.get("id"):
                # No id to rejoin the halves by: the row travels whole in bot.json rather than lose its run
                # state on every save (routine_add always mints one; this is a hand-written row).
                logging.getLogger(__name__).warning("bots: routine row without id kept whole in bot.json: %r",
                                                    row.get("task"))
                pub_rows.append(dict(row))
                continue
            pub_row = {k: v for k, v in row.items() if k in ROUTINE_PUBLIC_KEYS}
            pub_rows.append(pub_row)
            rest = {k: v for k, v in row.items() if k not in ROUTINE_PUBLIC_KEYS}
            # Every id'd row gets a private twin carrying the rev of the schedule it is saved beside (SCHED_REV_KEY).
            priv_rows.append({"id": row["id"], **rest, SCHED_REV_KEY: _sched_rev(pub_row)})
        public["routines"] = pub_rows
        if priv_rows:
            private["routines"] = priv_rows
    return public, private


def public_json(public: dict) -> str:
    """The public half as text: indented, key-sorted, newline-terminated, deterministic. What a package ships
    (package.py), so GEN_KEY is stripped: the gen is this machine's save bookkeeping, not part of the bot.
    bot.json on disk is this text plus the gen (_file_text)."""
    return json.dumps({k: v for k, v in public.items() if k != GEN_KEY}, indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def _file_text(public: dict, gen: str | None) -> str:
    """bot.json's exact on-disk text: public_json plus GEN_KEY (absent when None: a file from before the stamp
    keeps its text until its public content changes). write_meta compares this to decide whether to write."""
    body = {k: v for k, v in public.items() if k != GEN_KEY}
    if gen is not None:
        body[GEN_KEY] = gen
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_meta(bid: str, meta: dict) -> None:
    """Save the bot's meta: bot.json only when its public content changed, then state.json always (each
    atomically). The first rule keeps a bot folder git-clean: the runtime saves on every status flip and routine
    tick, and none of that may touch the public file. A changed bot.json gets a fresh GEN_KEY and state.json
    records the gen it was saved beside (PUBLIC_GEN_KEY), so a crash between the two replaces (new public, old
    private) reads back as torn (read_meta drops the schedule-derived run state) instead of a mismatched pair.
    A new bot's bot.json may briefly exist without state.json (empty private state: a valid bot). Raises
    LookupError when the bot has no folder (place() first)."""
    # One owner per workspace (registry's workspace routine claim) is what keeps the writers to one process.
    public, private = split_meta(meta)
    # bot.json's `id` is what paths.folders() keys the folder by: a missing or foreign one would lose the bot on
    # the next scan (or pose it as another), so the file always carries the bid it is saved under.
    if public.get("id") != bid:
        if public.get("id"):
            logging.getLogger(__name__).warning("bots: meta for %s carried id %r; saving it as %s",
                                                bid, public["id"], bid)
        public["id"] = bid
    path = meta_path(bid)
    spath = state_path(bid)
    gen = _write_public(path, public)
    private[PUBLIC_GEN_KEY] = gen
    write_json_atomic(spath, private)


def _write_public(path: str, public: dict) -> str | None:
    """Write bot.json unless its content (gen aside) is unchanged; returns the gen the file now carries."""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except (OSError, ValueError):
        existing = None  # missing, unreadable or not UTF-8: it differs, rewrite it
    if existing is not None:
        try:
            old = json.loads(existing)
        except ValueError:
            old = None
        old_gen = old.get(GEN_KEY) if isinstance(old, dict) else None
        if not isinstance(old_gen, str):
            old_gen = None
        if existing == _file_text(public, old_gen):
            return old_gen
    gen = uuid.uuid4().hex
    text = _file_text(public, gen)
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return gen


def list_ids(strict: bool = False) -> list[str]:
    """Every bot id with a bot.json, sorted (a folder place() made whose bot.json is not written yet is not a
    bot yet). `strict`: an unlistable bots root raises instead of reading as "no bots" (registry.all must not
    call an empty scan complete)."""
    found = _bpaths.folders(strict=strict)
    return sorted(bid for bid, folder in found.items() if os.path.isfile(os.path.join(folder, BOT_FILE)))


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
# Bumped under the lock by every write that changes what a summary would say (usage_log, usage_dismiss_failure, an
# outside reset). usage_summary reads the ledger under the lock and publishes its summary only if the generation is
# still the one it read: a write landing while the summary was being built would otherwise be hidden behind the
# stale snapshot for up to USAGE_CACHE_S. The prune in usage_summary rewrites the same content, so it does not bump.
_usage_gen = 0


def usage_log(bid, model, origin, task, ok, name=None, cost=None, input_tokens=None, error=None) -> None:
    """Append one call. The name travels with the line so a bot deleted later
    still shows up by name, not by id. `cost` (USD) and `input_tokens` are
    written only when known (the agent engine reports them; OpenBot did not).
    `error` is the failed call's own words (the CLI's result text, the API
    exception), written only on a failure so the "N failed" count in the page
    can open the reasons (usage_failures). A line may later gain `dismissed`
    (usage_dismiss_failure): the user cleared it from that list."""
    global _usage_cache, _usage_gen
    ev = {"ts": time.time(), "bot": bid, "name": name or "", "model": model, "origin": origin,
          "task": (task or "")[:80], "ok": bool(ok)}
    if cost is not None:
        ev["cost"] = cost
    if input_tokens is not None:
        ev["input_tokens"] = input_tokens
    if error and not ok:
        ev["error"] = str(error)[:2000]
    path = _bpaths.usage_path()
    with _usage_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        _usage_cache = (0.0, None)
        _usage_gen += 1


def usage_failures(bid: str, now: float | None = None) -> list[dict]:
    """The failed calls behind one bot's "N failed" count, newest first: the
    same window (USAGE_KEEP_DAYS) and the same `ok` test as usage_summary, so
    the list is exactly as long as the count the user clicked. Lines logged
    before the error text was recorded come back without `error`."""
    now = time.time() if now is None else now
    keep_from = now - USAGE_KEEP_DAYS * 86400
    out = []
    try:
        with open(_bpaths.usage_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("bot") != bid or ev.get("ok", True) or ev.get("ts", 0) < keep_from:
                    continue
                if ev.get("dismissed"):  # cleared from the dialog (usage_dismiss_failure); still a call in the totals
                    continue
                row = {"ts": ev.get("ts", 0), "model": ev.get("model") or "?", "origin": ev.get("origin") or "manual",
                       "task": ev.get("task") or ""}
                if ev.get("error"):
                    row["error"] = ev["error"]
                out.append(row)
    except FileNotFoundError:
        pass
    # By ts, not file position: usage_log stamps the line before it takes the lock, so two overlapping calls can land
    # in the file out of order.
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def usage_dismiss_failure(bid: str, ts: float) -> bool:
    """Clear one failure from the dialog (owner, 2026-10-13: "each row should have a clear icon, so that users can
    remove the old failure logs that they are not concerned about"). The line stays in the ledger with
    `dismissed: true` rather than being deleted: the call happened and cost tokens, so the today / 7-day totals and
    the model mix keep counting it; only the "N failed" count (usage_summary) and this dialog's list
    (usage_failures) drop it, together, so the count and the list stay the same length. Keyed by bot + ts: ts is
    time.time() at the call and round-trips JSON -> JS -> JSON exactly, so equality is the match, no epsilon (an
    epsilon could catch two overlapping calls at once). Read + rewrite inside _usage_lock, like the prune in
    usage_summary, so a line landing meanwhile is not lost. Idempotent: an unknown ts is a no-op (False)."""
    global _usage_cache, _usage_gen
    path = _bpaths.usage_path()
    hit = False
    with _usage_lock:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return False
        out = []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            if (isinstance(ev, dict) and ev.get("bot") == bid and ev.get("ts") == ts and not ev.get("ok", True)
                    and not ev.get("dismissed")):
                ev["dismissed"] = True
                line = json.dumps(ev) + "\n"
                hit = True
            out.append(line)
        if hit:
            tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(out)
            os.replace(tmp, path)
            _usage_cache = (0.0, None)
            _usage_gen += 1
    return hit


def usage_reset_cache() -> None:
    global _usage_cache, _usage_gen
    with _usage_lock:
        _usage_cache = (0.0, None)
        _usage_gen += 1  # an in-flight usage_summary must not publish over this reset either


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
    # Read and prune under the one lock: a snapshot taken outside it and written back after a usage_log or a
    # usage_dismiss_failure landed in between would drop that line (the cleared failure comes back, the call vanishes).
    with _usage_lock:
        gen = _usage_gen
        try:
            with open(path, encoding="utf-8") as f:
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
            tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
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
        failed = not ev.get("ok", True) and not ev.get("dismissed")  # a cleared failure is still a call, not a failure
        if failed:
            pb["errors"] += 1
        if t >= midnight:
            today += 1
            pb["today"] += 1
            per_task[ev.get("task", "")] = per_task.get(ev.get("task", ""), 0) + 1
            o = ev.get("origin") if ev.get("origin") in origin else "manual"
            origin[o] += 1
            if failed:
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
        with _usage_lock:
            if _usage_gen == gen:  # nothing landed while this was built; else the next call recomputes
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


_split = split_meta  # the name the first callers used
