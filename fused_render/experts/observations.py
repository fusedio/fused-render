"""The inbox, Notice's side (docs/experts.md "Files"): what capture saw in a session.

One JSON file per entry in `<expert>/observations/pending/`; Learn (`consolidate.py`) reads it there and moves
it to `observations/done/` with an `outcome`. Kinds: capture's (`repeated_request`, `user_correction`,
`error_then_fix`, `advice_confirmed`, `advice_contradicted`, `worked`, `failed`), and from Answer `gap` (an unsure consult), `taught`
and `forget` (chat; written by `consult.inbox_write`, the same file shape).
"""
from __future__ import annotations

import os
import time

from fused_render.experts import memory, store


def _dir(bot, which) -> str:
    p = os.path.join(store.expert_dir(bot), "observations", which)
    os.makedirs(p, exist_ok=True)
    return p


def _safe(obs_id) -> str:
    return os.path.basename(str(obs_id or ""))


QUOTE_MAX = 500


def write(bot, *, kind, claim, trust, session="", about=None, consult="", source=None, confirms="",
          contradicts="", by_user=None, quote="", after_advice=False, need="", tried="") -> dict:
    """`source`: where it came from when known, `{"target": str, "backend": str}` (the consulting or captured
    session's folder and engine), plus `canvas` (the canvas folder's name) and `files` (the paths that show a
    `worked` / `failed` entry) when given. `need` / `tried`: what a `failed` entry was after and the approach that
    did not work (stored only when given). `confirms`: the memory item id this observation sees again; stored only when
    that item exists (Learn then credits it without a model decision). `contradicts`: the memory item id
    this observation shows wrong or too broad (its claim is the corrected rule); validated the same way, except that
    a lesson already written into a skill (archived as `promoted`) may be named too (its skill then follows). `by_user`: capture proved the statement is the user's (its `quote` is a
    verbatim excerpt of a USER line); stored as given when True or False, left out when None (written by something
    that does not ground statements, or before grounding existed). `quote` is kept only with `by_user` True.
    `after_advice`: the user said it only after, and in line with, this expert's advice in that session (stored only
    when true; it does not raise a lesson's count)."""
    confirms = str(confirms or "").strip()
    contradicts = str(contradicts or "").strip()
    if confirms or contradicts:
        known = {i.get("id") for i in memory.items(bot)}
        confirms = confirms if confirms in known else ""
        if contradicts and contradicts not in known:
            contradicts = contradicts if contradicts in memory.promoted_ids(bot) else ""
    obs = {"id": store.new_id("o"), "ts": time.time(), "kind": str(kind or ""),
           "claim": " ".join(str(claim or "").split()), "trust": str(trust or "outcome"),
           "session": str(session or ""), "about": [str(a) for a in (about or []) if str(a).strip()],
           "consult": str(consult or "")}
    if confirms:
        obs["confirms"] = confirms
    if contradicts:
        obs["contradicts"] = contradicts
    if by_user is not None:
        obs["by_user"] = bool(by_user)
    quote = " ".join(str(quote or "").split())[:QUOTE_MAX]
    if by_user and quote:
        obs["quote"] = quote
    if after_advice:
        obs["after_advice"] = True
    for key, val in (("need", need), ("tried", tried)):
        val = " ".join(str(val or "").split())
        if val:
            obs[key] = val
    if isinstance(source, dict):
        src = {k: str(source.get(k) or "") for k in ("target", "backend")}
        if source.get("canvas"):
            src["canvas"] = str(source["canvas"])
        files = [str(f) for f in (source.get("files") or []) if isinstance(f, str) and f.strip()]
        if files:
            src["files"] = files
        if any(src.values()):
            obs["source"] = src
    with store.lock(bot):
        store.write_json(os.path.join(_dir(bot, "pending"), obs["id"] + ".json"), obs)
    return obs


def _read_dir(path) -> list:
    out = []
    try:
        names = os.listdir(path)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        o = store.read_json(os.path.join(path, n), None)
        if isinstance(o, dict) and o.get("id"):
            out.append(o)
    out.sort(key=lambda o: (float(o.get("ts") or 0), str(o.get("id"))))
    return out


def for_session(bot, session) -> list:
    """Every observation (pending and done) from task session `session`, oldest first."""
    sid = str(session or "")
    if not sid:
        return []
    rows = _read_dir(_dir(bot, "pending")) + _read_dir(_dir(bot, "done"))
    rows = [o for o in rows if o.get("session") == sid]
    rows.sort(key=lambda o: (float(o.get("ts") or 0), str(o.get("id"))))
    return rows


def pending(bot) -> list:
    """Oldest first."""
    return _read_dir(_dir(bot, "pending"))
