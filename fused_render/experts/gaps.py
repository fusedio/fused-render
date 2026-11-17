"""Learn, step 2: research on the questions the expert could not answer (docs/experts.md "research").

An unsure consult leaves a `gap` entry in the inbox. The lessons step records it here (`record`: the same
question after normalisation is one gap, counted once per distinct consult; `merge` folds a question asked in
other words into an older gap). `step` then researches up to `REPAIR_PER_RUN` open gaps with one deep, read-only
run each (`engine.research`):

- FOUND (the run cites a real file inside the expert's roots that it actually read) -> a `lesson_add` change,
  trust `research`, its source file in `evidence`; the gap is "answered".
- NOT FOUND -> `attempts += 1`, the gap stays "open", and a `note could_not_find` line says why.
- OUT OF ATTEMPTS (and nothing changed in the sources) -> one visible `note could_not_find`, once per gap
  (`reported` on the gap), so a question that will not be researched again is never only a quiet "noticed" line.

Nobody is asked anything. Research limits (`may_research`): `max_attempts` is a hard cap, at most one run per
gap per `RESEARCH_EVERY_S` (24 h), and a gap at the cap gets one more run only when the expert's skills or read
roots changed since its last attempt (`sources_fp`, stored as `research_fp`).

`<expert_dir>/gaps.json` = `{"version": 1, "gaps": [Gap]}` is Learn's own working state (which questions are
open and how often they were tried), written atomically under `store.lock(bot)`.
"""
from __future__ import annotations

import logging
import os
import time

from fused_render.experts import engine, memory, readtools, store

logger = logging.getLogger(__name__)

FILE = "gaps.json"
QUESTION_MAX = 1000
ANSWER_MAX = 2000
#: Gaps researched per Learn run.
REPAIR_PER_RUN = 2
#: One research run per gap per this many seconds.
RESEARCH_EVERY_S = 86400.0
#: Directory entries the read-root fingerprint looks at per root, at most.
_FP_ENTRIES_MAX = 4000


# ------------------------------------------------------------------ storage ---
def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), FILE)


def _load(bot) -> list:
    data = store.read_json(_path(bot), {})
    rows = data.get("gaps") if isinstance(data, dict) and isinstance(data.get("gaps"), list) else []
    rows = [g for g in rows if isinstance(g, dict) and g.get("id")]
    for g in rows:
        if g.get("status") != "answered":
            g["status"] = "open"  # statuses of the approval-gated design read as open
    return rows


def _save(bot, rows) -> None:
    store.write_json(_path(bot), {"version": 1, "gaps": rows})


def listing(bot) -> list:
    """Every gap: open ones first, most asked first."""
    with store.lock(bot):
        rows = _load(bot)
    rows.sort(key=lambda g: (g["status"] != "open", -int(g.get("count") or 0), -float(g.get("updated") or 0)))
    return rows


def open_gaps(bot) -> list:
    return [g for g in listing(bot) if g["status"] == "open"]


def _update(bot, gid, **fields) -> None:
    with store.lock(bot):
        rows = _load(bot)
        for g in rows:
            if g["id"] == gid:
                g.update(fields, updated=time.time())
        _save(bot, rows)


# ------------------------------------------------------------------- record ---
def record(bot, question, *, consult="", session="", origin="") -> dict:
    """A question the expert could not answer. The same question after normalisation (lowercase, collapsed
    whitespace, no trailing punctuation) bumps that gap's count (once per distinct consult) instead of adding one."""
    q = " ".join(str(question or "").split())[:QUESTION_MAX]
    if not q:
        raise ValueError("a gap needs a question")
    key = memory.normalise(q)
    consult, session = str(consult or ""), str(session or "")
    now = time.time()
    with store.lock(bot):
        rows = _load(bot)
        g = next((g for g in rows if key in {memory.normalise(x) for x in g.get("questions") or [g.get("question")]}),
                 None)
        if g is None:
            g = {"id": store.new_id("g"), "question": q, "questions": [q], "count": 0, "consults": [],
                 "sessions": [], "created": now, "status": "open", "attempts": 0, "finding": None, "error": ""}
            rows.append(g)
        if origin:
            g["origin"] = str(origin)  # "failed": a task could not do this (the log line says so)
        if not consult or consult not in g.setdefault("consults", []):
            g["count"] = int(g.get("count") or 0) + 1
        for k, v in (("consults", consult), ("sessions", session)):
            if v and v not in g.setdefault(k, []):
                g[k].append(v)
        g["updated"] = now
        _save(bot, rows)
        return dict(g)


def merge(bot, a, b):
    """Fold two open gaps that ask the same thing into the OLDER one: counts summed, lists united. Returns the
    kept gap, or None when nothing was merged."""
    if not a or not b or a == b:
        return None
    with store.lock(bot):
        rows = _load(bot)
        pair = [g for g in rows if g["id"] in (a, b) and g["status"] == "open"]
        if len(pair) != 2:
            return None
        keep, drop = sorted(pair, key=lambda g: (float(g.get("created") or 0), g["id"]))
        keep["count"] = int(keep.get("count") or 0) + int(drop.get("count") or 0)
        for k in ("questions", "consults", "sessions"):
            for v in drop.get(k) or []:
                if v and v not in keep.setdefault(k, []):
                    keep[k].append(v)
        keep["attempts"] = max(int(keep.get("attempts") or 0), int(drop.get("attempts") or 0))
        keep["updated"] = time.time()
        _save(bot, [g for g in rows if g is not drop])
        return dict(keep)


# ---------------------------------------------------------------- research ---
def _newest_mtime(root) -> int:
    """The newest mtime (ns) among `root` and its top two directory levels; at most `_FP_ENTRIES_MAX` entries."""
    newest, seen = 0, 0
    try:
        newest = os.stat(root).st_mtime_ns
    except OSError:
        return 0
    level = [root]
    for _depth in range(2):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        seen += 1
                        if seen > _FP_ENTRIES_MAX:
                            return newest
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        newest = max(newest, st.st_mtime_ns)
                        if e.is_dir(follow_symlinks=False):
                            nxt.append(e.path)
            except OSError:
                continue
        level = nxt
    return newest


def sources_fp(bot) -> str:
    """A cheap fingerprint of what research can read: each skill file's name, size and mtime, and for each read
    root (the manifest's `reads`) the newest mtime among its top two directory levels. No file is read."""
    parts = []
    sd = getattr(bot, "skills_dir", None)
    try:
        names = sorted(n for n in os.listdir(sd) if n.endswith(".md")) if sd else []
    except OSError:
        names = []
    for n in names:
        try:
            st = os.stat(os.path.join(sd, n))
            parts.append(f"s:{n}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"s:{n}:")
    try:
        roots = readtools.resolve_reads(store.manifest(bot))
    except Exception:  # noqa: BLE001 — unreadable reads: no roots
        roots = []
    for r in roots:
        parts.append(f"r:{r}:{_newest_mtime(r)}")
    return "|".join(parts)


def may_research(bot, g, now=None, fp=None) -> bool:
    """An open gap, not researched in the last `RESEARCH_EVERY_S`, with attempts < max_attempts; or, at the cap,
    once more when the skills or read roots changed since its last attempt (`sources_fp` differs from the stored
    `research_fp`)."""
    now = time.time() if now is None else now
    if g.get("status") != "open" or now - float(g.get("researched_at") or 0) < RESEARCH_EVERY_S:
        return False
    if int(g.get("attempts") or 0) < store.manifest(bot)["gaps"]["max_attempts"]:
        return True
    stored = str(g.get("research_fp") or "")
    return bool(stored) and (fp or sources_fp(bot)) != stored


def repairable(bot) -> list:
    """The gaps this run researches: the open gaps that `may_research`, most asked first, at most
    `REPAIR_PER_RUN`. [] when the manifest's `gaps.research` is off."""
    if not store.manifest(bot)["gaps"]["research"]:
        return []
    now, fp = time.time(), sources_fp(bot)
    rows = [g for g in listing(bot) if may_research(bot, g, now, fp)]
    rows.sort(key=lambda g: (-int(g.get("count") or 0), float(g.get("created") or 0)))
    return rows[:REPAIR_PER_RUN]


def gave_up(bot) -> list:
    """The open gaps that are out of attempts, whose sources did not change since the last attempt (so
    `may_research` skips them for good) and that were not reported yet. [] when research is off."""
    m = store.manifest(bot)["gaps"]
    if not m["research"]:
        return []
    fp, out = None, []
    for g in listing(bot):
        if g["status"] != "open" or g.get("reported") or int(g.get("attempts") or 0) < m["max_attempts"]:
            continue
        stored = str(g.get("research_fp") or "")
        fp = sources_fp(bot) if stored and fp is None else fp
        if not stored or fp == stored:
            out.append(g)
    return out


def accept(bot, res) -> dict | None:
    """A research result as a finding `{"answer","refs"}`, or None: `found` true, a non-empty answer, and at least
    one ref that is an existing file inside the expert's roots which the run actually read."""
    if not isinstance(res, dict) or not res.get("found"):
        return None
    ans = str(res.get("answer") or "").strip()
    if not ans:
        return None
    roots = engine.roots(bot)
    read = {os.path.realpath(p) for p in (res.get("read") or []) if isinstance(p, str)}
    good = []
    for r in res.get("refs") or []:
        try:
            real = os.path.realpath(os.path.expanduser(str(r)))
        except Exception:  # noqa: BLE001
            continue
        if os.path.isfile(real) and real in read and readtools._inside(real, roots) and real not in good:
            good.append(real)
    return {"answer": ans[:ANSWER_MAX], "refs": good} if good else None


def step(bot, ctx):
    """Step 2 of a Learn run: one research run per repairable gap; found -> a lesson, not found -> a note. A gap
    that is out of attempts and will not be researched again gets one visible note, once."""
    for g in gave_up(bot):
        line = yield {"op": "note", "action": "could_not_find",
                      "summary": f"Could not find an answer to: {g['question'][:300]}",
                      "why": f"I looked {int(g.get('attempts') or 0)} times and will look again only when my "
                             "skills or folders change.",
                      "evidence": {"item": g["id"], "session": (g.get("sessions") or [""])[-1]}}
        if line is None or line.get("ok"):
            _update(bot, g["id"], reported=True)
    for g in repairable(bot):
        _update(bot, g["id"], researched_at=time.time(), research_fp=sources_fp(bot))
        try:
            res = engine.research(bot, list(g.get("questions") or [g["question"]]))
            finding = accept(bot, res)
            error = "" if finding else (
                ("not found: " + str(res.get("why") or "")).strip(": ")[:500] if not res.get("found")
                else "the answer cited no file the research actually read inside the expert's folders")
        except Exception as e:  # noqa: BLE001 — a failed run counts as an attempt
            logger.warning("expert %s: research run for gap %s failed", bot.id, g["id"], exc_info=True)
            finding, error = None, f"the research run failed: {str(e) or type(e).__name__}"
        evidence = {"item": g["id"], "session": (g.get("sessions") or [""])[-1]}
        if finding:
            line = yield {"op": "lesson_add", "claim": f"When asked: {g['question']} Answer: {finding['answer']}",
                          "kind": "lesson", "trust": "research", "about": finding["refs"],
                          "why": ("A task could not do this, so I looked for what works." if g.get("origin") == "failed"
                                  else "A task asked this and I was not sure, so I looked it up."),
                          "evidence": {**evidence, "source": finding["refs"][0]}}
            if line.get("ok"):
                _update(bot, g["id"], status="answered", finding=finding, error="")
                continue
            error = str(line.get("error") or "the answer could not be stored")
        attempts = int(g.get("attempts") or 0) + 1
        # The note below is the visible line for a gap that is now out of attempts.
        _update(bot, g["id"], attempts=attempts, error=error,
                **({"reported": True} if attempts >= store.manifest(bot)["gaps"]["max_attempts"] else {}))
        yield {"op": "note", "action": "could_not_find", "summary": f"Could not find an answer to: {g['question'][:300]}",
               "why": error[:300], "evidence": evidence}
