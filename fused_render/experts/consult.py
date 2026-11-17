"""Consults: a task session asks an expert one question (docs/experts.md §4 `consult.py`, §6, §7).

A consult is a separate, short run of the expert (`engine.answer`, profile
"consult"). It never touches the expert's chat thread and never writes memory:
the question and context are data from an agent. An unsure answer leaves a
`gap` entry in the inbox (`inbox_write`, Answer's only way into the inbox; chat uses it for `taught` / `forget`). At most `MAX_PARALLEL` consults
run at once in this process; the rest wait up to `SLOT_WAIT_S` for a turn and
then fail with "the expert is busy". The caller's target folder becomes
readable for the consult, unless it is `/`, the home folder or an ancestor of
it (`readtools.too_broad`). A consult may make at most `engine.CONSULT_TOOL_CAP`
tool calls; the record keeps how many it made (`tool_calls`) and whether it hit
the cap (`capped`).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from fused_render.experts import engine, memory, model, readtools, store

logger = logging.getLogger(__name__)

MAX_PARALLEL = 3
SLOT_WAIT_S = 120
BUSY = "the expert is busy; try again in a minute"
_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)
CONSULTS_FILE = "consults.jsonl"
CALLER_KEYS = ("run_id", "session_id", "backend", "target", "canvas")


def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), CONSULTS_FILE)


def _caller(caller) -> dict:
    c = caller if isinstance(caller, dict) else {}
    return {k: c[k] for k in CALLER_KEYS if c.get(k) not in (None, "")}


def inbox_write(bot, kind, claim, **fields) -> dict:
    """One inbox entry from Answer: `gap` (an unsure consult), `taught` or `forget` (chat, `by_user=True` only
    for a message typed at the computer). Learn reads it from `observations/pending/`."""
    obs = {"id": store.new_id("o"), "ts": time.time(), "kind": kind, "claim": " ".join(str(claim or "").split()),
           "trust": "gap" if kind == "gap" else "user", **fields}
    with store.lock(bot):
        store.write_json(os.path.join(store.expert_dir(bot), "observations", "pending", obs["id"] + ".json"), obs)
    return obs


def consult(bot, question, context="", caller=None) -> dict:
    """{"id","answer","refs","used","unsure"}. Raises model.Unavailable when no engine is on; a model failure
    raises (RuntimeError) and is not logged as a consult."""
    question = str(question or "").strip()
    context = str(context or "")
    who = _caller(caller)
    ok, reason = model.available(bot)
    if not ok:
        raise model.Unavailable(reason)
    extra = []
    target = who.get("target")
    if isinstance(target, str):
        t = os.path.expanduser(target)
        if os.path.isabs(t) and os.path.isdir(t):
            if readtools.too_broad(t):
                logger.warning("expert %s: not reading the caller's target %s (too broad)", bot.id, t)
            else:
                extra.append(t)
    t0 = time.monotonic()
    if not _SLOTS.acquire(timeout=SLOT_WAIT_S):
        raise RuntimeError(BUSY)
    try:
        stats: dict = {}
        out = engine.answer(bot, question, profile="consult", context=context, extra_roots=extra, stats=stats)
    finally:
        _SLOTS.release()
    ms = int((time.monotonic() - t0) * 1000)
    cid = store.new_id("c")
    rec = {"id": cid, "ts": time.time(), "question": question, "context": context, "answer": out["answer"],
           "refs": list(out["refs"]), "used": list(out["used"]), "unsure": bool(out["unsure"]), "caller": who, "ms": ms,
           "tool_calls": int(stats.get("tool_calls") or 0), "capped": bool(stats.get("capped"))}
    try:
        with store.lock(bot):
            store.append_jsonl(_path(bot), rec)
    except OSError:
        logger.warning("expert %s: consult %s not logged", bot.id, cid, exc_info=True)
    if rec["used"]:
        try:
            memory.cite(bot, rec["used"])  # the lessons this answer relied on (the memory budget's usage)
        except Exception:  # noqa: BLE001 — the answer still goes back to the agent
            logger.warning("expert %s: lessons of consult %s not cited", bot.id, cid, exc_info=True)
    if rec["unsure"]:
        try:
            inbox_write(bot, "gap", f"Could not answer: {question[:500]}",
                        session=str(who.get("session_id") or ""), about=list(rec["refs"]), consult=cid,
                        source={"target": who.get("target") or "", "backend": who.get("backend") or ""})
        except Exception:  # noqa: BLE001 — the answer still goes back to the agent
            logger.warning("expert %s: gap entry for consult %s not written", bot.id, cid, exc_info=True)
    return {"id": cid, "answer": rec["answer"], "refs": rec["refs"], "used": rec["used"], "unsure": rec["unsure"]}


def log(bot, limit=100) -> list:
    """The newest `limit` consult records, NEWEST FIRST (what `GET /api/experts/{id}/consults` answers)."""
    return list(reversed(store.read_jsonl(_path(bot), limit=limit)))
