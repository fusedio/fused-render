"""Gap records: the repair half of consolidation (docs/experts.md §4 `gaps.py`, §11).

An unsure consult writes a `gap` observation (`consult.py`), one kind of
observation among the others. `consolidate.run` folds gap observations into
gap records here (`record`: the same question after normalisation is one
gap, counted once per distinct consult; the model may `merge` a question
asked in other words into an older gap), then repairs up to `REPAIR_PER_RUN`
open gaps per run with one deep, read-only run each (`research`, profile
"research"):

- FOUND (the run cites a real file inside its roots that it actually read) →
  a memory lesson with trust "research" (linked by `gap`; not loaded into
  answers until its skill fix is approved) and, at once, a skill proposal for
  it (`proposals.request(gap=)`); the gap is "proposed". Approve → "answered"
  (resolution "skill"); reject or a failed proposal → "dismissed".
- NOT FOUND → `attempts += 1`, the gap stays "open". It may be asked in the
  expert's chat (`cards.ask_gap`, status "asked" while the card is up) once
  it was seen `ask_after` times or tried `max_attempts` times (`askable`).
- The user's own answer, typed at the Mac, becomes a `user` lesson
  (`answer`); "skip" dismisses it.

Research limits (`may_research`, every path, `research` itself included):
`max_attempts` is a hard cap (a gap touched again in a later run is NOT
researched past it), at most one run per gap per `RESEARCH_EVERY_S` (24 h),
and a gap at the cap is only askable, unless the expert's skills or read roots
changed since its last attempt (`sources_fp`, stored as `research_fp`): then
it gets one more run. A gap with `snoozed_until` in the future is neither
researched nor asked.

Research never writes a skill: only the user's Approve on the proposal card
does, and its lesson stays out of answers until then.

`<expert_dir>/gaps.json` = `{"version": 1, "gaps": [Gap], "last_asked": {"at", "gap"}}`, written atomically
under `store.lock(bot)`. "researching" / "asked" that no live run / card in this process holds read as "open".
"""
from __future__ import annotations

import logging
import os
import threading
import time

from fused_render.experts import store

logger = logging.getLogger(__name__)

FILE = "gaps.json"
STATUSES = ("open", "researching", "proposed", "asked", "answered", "dismissed")
CLOSED = ("answered", "dismissed")
QUESTION_MAX = 1000
ANSWER_MAX = 2000
LIST_MAX = 50
TRANSCRIPT_CAP = 12000
BUSY = "research is already running for this expert"
#: Gaps researched per consolidation run.
REPAIR_PER_RUN = 2
#: One research run per gap per this many seconds, whatever path asks.
RESEARCH_EVERY_S = 86400.0
#: Directory entries the read-root fingerprint looks at per root, at most.
_FP_ENTRIES_MAX = 4000

_GUARD = threading.Lock()
#: bot id -> the gap id being researched in this process.
_RESEARCHING: dict[str, str] = {}
#: bot id -> the gap id whose question card is up (cards.ask_gap's worker).
_CARDS: dict[str, str] = {}


# ------------------------------------------------------------------ storage ---
def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), FILE)


def _load(bot) -> dict:
    data = store.read_json(_path(bot), {})
    if not isinstance(data, dict):
        data = {}
    rows = data.get("gaps") if isinstance(data.get("gaps"), list) else []
    data["gaps"] = [g for g in rows if isinstance(g, dict) and g.get("id")]
    if not isinstance(data.get("last_asked"), dict):
        data["last_asked"] = {"at": 0, "gap": ""}
    return data


def _save(bot, data) -> None:
    store.write_json(_path(bot), {"version": 1, "gaps": data["gaps"], "last_asked": data["last_asked"]})


def _find(data, gid):
    for g in data["gaps"]:
        if g.get("id") == gid:
            return g
    return None


def _norm(q) -> str:
    from fused_render.experts import memory
    return memory.normalise(q)


def _settings(bot) -> dict:
    return store.manifest(bot)["gaps"]


def researching(bot):
    """The gap id this process is researching for `bot`, or None."""
    with _GUARD:
        return _RESEARCHING.get(str(bot.id))


def card_up(bot):
    """The gap id whose question card is up for `bot`, or None."""
    with _GUARD:
        return _CARDS.get(str(bot.id))


def _view(bot, g) -> dict:
    """A copy for readers: a "researching" gap that no research in this process is working on (the app quit
    mid-run) reads as "open"."""
    g = dict(g)
    for k, v in (("questions", []), ("consults", []), ("sessions", []), ("attempts", 0), ("finding", None),
                 ("proposal", None), ("card_seq", None), ("resolution", ""), ("error", ""),
                 ("researched_at", 0), ("research_fp", "")):
        g.setdefault(k, v)
    if g.get("status") == "researching" and researching(bot) != g["id"]:
        g["status"] = "open"
    if g.get("status") == "asked" and card_up(bot) != g["id"]:
        g["status"], g["card_seq"] = "open", None
    return g


def with_proposal(bot, rows) -> list:
    """Copies of the Gaps `rows` (None entries kept) for HTTP, each with `proposal_status` (its linked
    proposal's status, "" when it has none or the proposal cannot be found) and `proposal_error` (that
    proposal's `error`, else ""). Read from the proposals store now; never persisted on the gap."""
    from fused_render.experts import proposals
    cache: dict = {}
    out = []
    for g in rows:
        if not isinstance(g, dict):
            out.append(g)
            continue
        pid = g.get("proposal")
        if pid and pid not in cache:
            try:
                cache[pid] = proposals.get(bot, pid)
            except Exception:  # noqa: BLE001 — an unreadable proposal reads as none
                cache[pid] = None
        p = cache.get(pid) if pid else None
        out.append(dict(g, proposal_status=str((p or {}).get("status") or ""),
                        proposal_error=str((p or {}).get("error") or "")))
    return out


def _rank(g) -> tuple:
    return (g.get("status") in CLOSED, -int(g.get("count") or 0), -float(g.get("updated") or 0))


def listing(bot) -> list:
    """Every gap: open ones first, most asked first."""
    with store.lock(bot):
        rows = [_view(bot, g) for g in _load(bot)["gaps"]]
    rows.sort(key=_rank)
    return rows


def get(bot, gid):
    if not gid:
        return None
    with store.lock(bot):
        g = _find(_load(bot), str(gid))
        return _view(bot, g) if g is not None else None


def ids(bot) -> set:
    with store.lock(bot):
        return {g["id"] for g in _load(bot)["gaps"]}


def _update(bot, gid, **fields):
    with store.lock(bot):
        data = _load(bot)
        g = _find(data, gid)
        if g is None:
            return None
        g.update(fields, updated=time.time())
        _save(bot, data)
        return _view(bot, g)


# ------------------------------------------------------------------- record ---
def record(bot, question, *, consult="", session="") -> dict:
    """A question the expert could not answer. The same question after normalisation (lowercase, collapsed
    whitespace, no trailing punctuation) bumps that gap's count instead of adding one."""
    q = " ".join(str(question or "").split())[:QUESTION_MAX]
    if not q:
        raise ValueError("a gap needs a question")
    key = _norm(q)
    consult, session = str(consult or ""), str(session or "")
    now = time.time()
    with store.lock(bot):
        data = _load(bot)
        for g in data["gaps"]:
            if key in {_norm(x) for x in ([g.get("question")] + list(g.get("questions") or []))}:
                if not consult or consult not in (g.get("consults") or []):
                    g["count"] = int(g.get("count") or 0) + 1
                for k, v in (("consults", consult), ("sessions", session)):
                    if v and v not in g.setdefault(k, []):
                        g[k].append(v)
                g["updated"] = now
                _save(bot, data)
                return _view(bot, g)
        g = {"id": store.new_id("g"), "question": q, "questions": [q], "count": 1,
             "consults": [consult] if consult else [], "sessions": [session] if session else [],
             "created": now, "updated": now, "status": "open", "attempts": 0, "finding": None, "proposal": None,
             "card_seq": None, "resolution": "", "error": ""}
        data["gaps"].append(g)
        _save(bot, data)
        return _view(bot, g)


def merge(bot, a, b):
    """Fold two gaps that ask the same thing into the OLDER one: counts summed, lists united. Only gaps that are
    still open (not closed, not proposed, not being researched or on a card). Returns the kept gap, or None when
    nothing was merged."""
    if not a or not b or a == b:
        return None
    with store.lock(bot):
        data = _load(bot)
        ga, gb = _find(data, a), _find(data, b)
        if ga is None or gb is None:
            return None
        busy = {researching(bot), card_up(bot)}
        for g in (ga, gb):
            if g.get("status") not in ("open", "asked", "researching") or g["id"] in busy:
                return None
        keep, drop = sorted((ga, gb), key=lambda g: (float(g.get("created") or 0), g["id"]))
        keep["count"] = int(keep.get("count") or 0) + int(drop.get("count") or 0)
        for k in ("questions", "consults", "sessions"):
            for v in list(drop.get(k) or []) + ([drop.get("question")] if k == "questions" else []):
                if v and v not in keep.setdefault(k, []):
                    keep[k].append(v)
        keep["attempts"] = max(int(keep.get("attempts") or 0), int(drop.get("attempts") or 0))
        if not keep.get("finding") and drop.get("finding"):
            keep["finding"] = drop["finding"]
        if drop.get("status") == "asked" and keep.get("status") != "asked":
            keep["status"], keep["card_seq"] = "asked", None
        if keep.get("status") == "researching":
            keep["status"] = "open"
        keep["updated"] = time.time()
        data["gaps"] = [g for g in data["gaps"] if g is not drop]
        _save(bot, data)
        return _view(bot, keep)


def for_consult(bot, consult_id):
    """The gap that records consult `consult_id`, or None."""
    if not consult_id:
        return None
    with store.lock(bot):
        for g in _load(bot)["gaps"]:
            if consult_id in (g.get("consults") or []):
                return _view(bot, g)
    return None


def open_gaps(bot) -> list:
    """Gaps consolidation may merge into: not closed, not proposed."""
    return [g for g in listing(bot) if g["status"] in ("open", "asked", "researching")]


# ------------------------------------------------------------------ settle ---
def snooze(bot, gid, until):
    """Put gap `gid` off until `until` (epoch seconds): "open" again, off any card, neither researched nor asked
    before then (`snoozed`). Returns the gap, or None when it is unknown."""
    return _update(bot, str(gid or ""), status="open", card_seq=None, snoozed_until=float(until))


def dismiss(bot, gid) -> dict:
    g = _update(bot, str(gid or ""), status="dismissed", resolution="dismissed", card_seq=None)
    if g is None:
        raise ValueError("no such gap")
    return g


def answer(bot, gid, text) -> dict:
    """The user's own answer (typed at the Mac): a `user` lesson in memory, the gap answered."""
    from fused_render.experts import memory
    text = " ".join(str(text or "").split())[:ANSWER_MAX]
    if not text:
        raise ValueError("an answer needs some text")
    g = get(bot, gid)
    if g is None:
        raise ValueError("no such gap")
    item = memory.add(bot, f"When asked \"{g['question']}\": {text}", trust="user")
    out = _update(bot, g["id"], status="answered", resolution="user", card_seq=None, error="",
                  finding={"answer": text, "refs": []})
    out = dict(out or g)
    out["lesson"] = item
    return out


def proposal_settled(bot, gid, pid, *, approved) -> None:
    """`proposals` calls this when a proposal drafted from gap `gid` is approved, rejected or failed."""
    with store.lock(bot):
        data = _load(bot)
        g = _find(data, gid)
        if g is None or (pid and g.get("proposal") not in (None, pid)):
            return
        if approved:
            g.update(status="answered", resolution="skill", error="")
        else:
            g.update(status="dismissed", resolution="dismissed")
        g["proposal"] = pid or g.get("proposal")
        g["updated"] = time.time()
        _save(bot, data)


# ---------------------------------------------------------------- research ---
def snoozed(g, now=None) -> bool:
    """The gap carries a `snoozed_until` in the future: neither researched nor asked until then."""
    try:
        until = float(g.get("snoozed_until") or 0)
    except (TypeError, ValueError):
        return False
    return until > (time.time() if now is None else now)


def _stat_sig(path) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


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
    from fused_render.experts import readtools
    parts = []
    sd = getattr(bot, "skills_dir", None)
    try:
        names = sorted(n for n in os.listdir(sd) if n.endswith(".md")) if sd else []
    except OSError:
        names = []
    for n in names:
        parts.append(f"s:{n}:{_stat_sig(os.path.join(sd, n))}")
    try:
        roots = readtools.resolve_reads(store.manifest(bot))
    except Exception:  # noqa: BLE001 — unreadable reads: no roots
        roots = []
    for r in roots:
        parts.append(f"r:{r}:{_newest_mtime(r)}")
    return "|".join(parts)


def may_research(bot, g, now=None, fp=None) -> bool:
    """The research rules every path obeys (docs/experts.md §11): an open gap, not on a card, not snoozed, not
    researched in the last `RESEARCH_EVERY_S`, and attempts < max_attempts, or, at the cap, once more when the
    skills or read roots changed since its last attempt (`sources_fp` differs from the stored `research_fp`).
    `fp` is a callable returning the current fingerprint (computed only when needed)."""
    now = time.time() if now is None else now
    if g.get("status") != "open" or g["id"] == card_up(bot) or snoozed(g, now):
        return False
    if now - float(g.get("researched_at") or 0) < RESEARCH_EVERY_S:
        return False
    if int(g.get("attempts") or 0) < _settings(bot)["max_attempts"]:
        return True
    stored = str(g.get("research_fp") or "")
    if not stored or g.get("finding"):
        return False
    return (fp() if fp else sources_fp(bot)) != stored


def repairable(bot, touched=()) -> list:
    """The gaps consolidation researches this run (step c): the open gaps that `may_research` (being touched in
    this run no longer bypasses the cap); most asked first, at most `REPAIR_PER_RUN`. [] when `research` is
    off. `touched` is kept for callers and does not change the choice."""
    s = _settings(bot)
    if not s["research"]:
        return []
    now = time.time()
    cache: list = []

    def fp():
        if not cache:
            cache.append(sources_fp(bot))
        return cache[0]
    rows = [g for g in listing(bot) if may_research(bot, g, now, fp)]
    rows.sort(key=lambda g: (-int(g.get("count") or 0), float(g.get("created") or 0)))
    return rows[:REPAIR_PER_RUN]


def closed_ids(bot) -> set:
    with store.lock(bot):
        return {g["id"] for g in _load(bot)["gaps"] if g.get("status") in CLOSED}


def mark_proposed(bot, gid, pid) -> None:
    """`proposals.request` started a proposal that carries this gap's research lesson."""
    with store.lock(bot):
        data = _load(bot)
        g = _find(data, gid)
        if g is None or g.get("status") in CLOSED:
            return
        g.update(status="proposed", proposal=pid, error="", updated=time.time())
        _save(bot, data)


def _claim(bot, gid) -> bool:
    with _GUARD:
        if str(bot.id) in _RESEARCHING:
            return False
        _RESEARCHING[str(bot.id)] = gid
        return True


def _release(bot, gid) -> None:
    with _GUARD:
        if _RESEARCHING.get(str(bot.id)) == gid:
            _RESEARCHING.pop(str(bot.id), None)


def research(bot, gid) -> dict:
    """Internal (consolidation's repair step): one deep run for open gap `gid`, synchronously, then act on it
    (module docstring). ValueError when the gap is unknown, not open, or research already runs for this expert."""
    gid = str(gid or "")
    g = get(bot, gid)
    if g is None:
        raise ValueError("no such gap")
    if g["status"] != "open" or card_up(bot) == gid:
        raise ValueError(f"this gap is {g['status']}; it is not researched now")
    if not may_research(bot, g):
        raise ValueError("this gap is not researched now (attempt cap, once a day, or snoozed)")
    if not _claim(bot, gid):
        raise ValueError(BUSY)
    try:
        return _research(bot, gid)
    finally:
        _release(bot, gid)


def _session_tail(gap) -> str:
    """What the asking session did after its consult: the tail of its transcript (capture's renderer), capped
    at TRANSCRIPT_CAP. "" when there is no session or it cannot be read."""
    sids = [s for s in (gap.get("sessions") or []) if s]
    if not sids:
        return ""
    try:
        from fused_render.experts import capture
        session = capture.session_by_id(sids[-1])
        if session is None:
            return ""
        history = capture._agent()._history(session.get("target") or "", session["session_id"],
                                            transcript=session.get("path") or "")
        turns = list(history.get("turns") or []) if isinstance(history, dict) else []
        start = 0
        for i, turn in enumerate(turns):
            for seg in (turn.get("segments") or []) if isinstance(turn, dict) else []:
                if isinstance(seg, dict) and seg.get("kind") == "tool" and "expert_consult" in str(seg.get("name")):
                    start = i
        md = capture.render_turns(turns[start:])
    except Exception:  # noqa: BLE001 — research goes on without it
        logger.debug("gap %s: session transcript unavailable", gap.get("id"), exc_info=True)
        return ""
    if len(md) > TRANSCRIPT_CAP:
        tail = md[-TRANSCRIPT_CAP:]
        nl = tail.find("\n")
        md = "[... earlier conversation cut ...]\n" + (tail[nl + 1:] if nl != -1 else tail)
    return md


def accept(bot, res) -> dict | None:
    """A research result as a finding `{"answer","refs"}`, or None: `found` true, a non-empty answer, and at least
    one ref that is an existing file inside the expert's roots which the run actually read."""
    from fused_render.experts import engine, readtools
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
    if not good:
        return None
    return {"answer": ans[:ANSWER_MAX], "refs": good}


def lesson_text(gap, finding) -> str:
    return f"When asked: {gap['question']} Answer: {finding['answer']}"


def _research(bot, gid) -> dict:
    from fused_render.experts import engine, memory, proposals
    g = _update(bot, gid, status="researching", error="", researched_at=time.time(), research_fp=sources_fp(bot))
    error = ""
    try:
        res = engine.research(bot, list(g.get("questions") or [g["question"]]), transcript=_session_tail(g))
        finding = accept(bot, res)
        if finding is None:
            error = (("not found: " + str(res.get("why") or "")).strip(": ")[:500] if not res.get("found")
                     else "the answer cited no file the research actually read inside the expert's folders")
    except Exception as e:  # noqa: BLE001 — a failed run counts as an attempt
        logger.warning("expert %s: research run for gap %s failed", getattr(bot, "id", "?"), gid, exc_info=True)
        finding, error = None, f"the research run failed: {str(e) or type(e).__name__}"
    with store.lock(bot):
        # The run took a while: the user may have answered or dismissed the gap meanwhile (a card, the API).
        # Their decision stands; only a gap still "researching" takes this run's result.
        cur = _find(_load(bot), gid)
        if cur is None or cur.get("status") != "researching":
            return get(bot, gid)
        if finding:
            # The finding is a lesson (trust "research": not in answers until its skill fix is approved) and,
            # at once, a skill fix. When a proposal is already being prepared, `proposals.candidates` picks the
            # lesson up on the next run.
            item = memory.add(bot, lesson_text(g, finding), kind="lesson", trust="research",
                              about=finding["refs"], gap=gid)
            _update(bot, gid, status="proposed", finding=finding, error="")
        else:
            return _update(bot, gid, status="open", attempts=int(cur.get("attempts") or 0) + 1, card_seq=None,
                           error=error or "not found")
    if finding:
        try:
            proposals.request(bot, [item], topic=f"Gap: {g['question']}"[:200], gap=gid)
        except ValueError as e:
            _update(bot, gid, error=f"skill fix waits for the next run: {e}")
    return get(bot, gid)


# ------------------------------------------------------------------- asking ---
def askable(bot, g) -> bool:
    """An open gap the user may be asked about: seen `ask_after` times or tried `max_attempts` times, and
    researched at least once (unless research is off)."""
    s = _settings(bot)
    if g.get("status") != "open" or snoozed(g):
        return False
    attempts = int(g.get("attempts") or 0)
    if s["research"] and attempts < 1:
        return False
    return int(g.get("count") or 0) >= s["ask_after"] or attempts >= s["max_attempts"]


def to_ask(bot):
    """The gap whose question should go up now (`askable`, not being researched or on a card), at most one new
    gap question per `ask_every_s` (re-raising the last one asked is not a new question). None otherwise."""
    every = _settings(bot)["ask_every_s"]
    busy = {card_up(bot), researching(bot)}
    with store.lock(bot):
        last = _load(bot)["last_asked"]
    rows = [g for g in listing(bot) if g["id"] not in busy and askable(bot, g)]
    now = time.time()
    for g in rows:  # most asked first
        if g["id"] == last.get("gap") or now - float(last.get("at") or 0) >= every:
            return g
    return None


def card_claim(bot, gid) -> bool:
    with _GUARD:
        if str(bot.id) in _CARDS:
            return False
        _CARDS[str(bot.id)] = gid
        return True


def card_release(bot, gid) -> None:
    with _GUARD:
        if _CARDS.get(str(bot.id)) == gid:
            _CARDS.pop(str(bot.id), None)


def mark_asked(bot, gid, seq) -> None:
    with store.lock(bot):
        data = _load(bot)
        g = _find(data, gid)
        if g is None:
            return
        g.update(status="asked", card_seq=seq, updated=time.time())
        data["last_asked"] = {"at": time.time(), "gap": gid}
        _save(bot, data)


def put_aside(bot, gid, *, stopped=False) -> None:
    """The card went down unanswered: the gap is "open" again (to be asked again). After a Stop the throttle
    applies to it again."""
    with store.lock(bot):
        data = _load(bot)
        g = _find(data, gid)
        if g is None:
            return
        if g.get("status") == "asked":
            g.update(status="open", card_seq=None)
        if stopped and data["last_asked"].get("gap") == gid:
            data["last_asked"]["gap"] = ""
        _save(bot, data)
