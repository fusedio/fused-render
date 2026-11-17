"""Raw observations awaiting consolidation (docs/experts.md §4 `observations.py`).

One JSON file per observation in `<expert>/observations/pending/`; consolidation
moves it to `observations/done/`.
"""
from __future__ import annotations

import os
import time

from fused_render.experts import store


def _dir(bot, which) -> str:
    p = os.path.join(store.expert_dir(bot), "observations", which)
    os.makedirs(p, exist_ok=True)
    return p


def _safe(obs_id) -> str:
    return os.path.basename(str(obs_id or ""))


def write(bot, *, kind, claim, trust, session="", about=None, consult="", source=None, confirms="",
          contradicts="", by_user=False) -> dict:
    """`source`: where it came from when known, `{"target": str, "backend": str}` (the consulting or captured
    session's folder and engine). `confirms`: the memory item id this observation sees again; stored only when
    that item exists (consolidation then bumps it without a model decision). `contradicts`: the memory item id
    this observation shows wrong or too broad (its claim is the corrected rule); validated the same way
    (consolidation then counts a contradiction on it, never a bump). `by_user`: a USER line in the session said
    the advice was wrong (stored only when true; the corrected rule then becomes a `user_correction` lesson)."""
    confirms = str(confirms or "").strip()
    contradicts = str(contradicts or "").strip()
    if confirms or contradicts:
        from fused_render.experts import memory
        known = {i.get("id") for i in memory.items(bot)}
        confirms = confirms if confirms in known else ""
        contradicts = contradicts if contradicts in known else ""
    obs = {"id": store.new_id("o"), "ts": time.time(), "kind": str(kind or ""),
           "claim": " ".join(str(claim or "").split()), "trust": str(trust or "outcome"),
           "session": str(session or ""), "about": [str(a) for a in (about or []) if str(a).strip()],
           "consult": str(consult or "")}
    if confirms:
        obs["confirms"] = confirms
    if contradicts:
        obs["contradicts"] = contradicts
    if by_user:
        obs["by_user"] = True
    if isinstance(source, dict):
        src = {k: str(source.get(k) or "") for k in ("target", "backend")}
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


def pending(bot) -> list:
    """Oldest first."""
    return _read_dir(_dir(bot, "pending"))


DONE_LIMIT = 200


def done(bot, limit=DONE_LIMIT) -> list:
    """Archived observations, NEWEST first, at most `limit` (None: all), each with the `outcome` consolidation
    recorded on it."""
    rows = list(reversed(_read_dir(_dir(bot, "done"))))
    return rows if limit is None else rows[:max(0, int(limit))]


OUTCOME_OPS = ("add", "bump", "nothing", "gap", "contradict")


def archive(bot, ids, outcomes=None) -> None:
    """pending/ -> done/. `outcomes` maps an observation id to what consolidation did with it:
    `{"op": "add"|"bump"|"nothing"|"gap"|"contradict", "item"?: memory item id, "gap"?: gap id, "new"?: memory
    item id}`; it is stored on the archived record as `outcome` (with `at`). `contradict`: `item` is the lesson
    the observation contradicted, `new` the lesson its corrected rule landed on. An id without one gets
    `{"op": "nothing"}`."""
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    now = time.time()
    with store.lock(bot):
        src, dst = _dir(bot, "pending"), _dir(bot, "done")
        for oid in ids or []:
            name = _safe(oid)
            if not name:
                continue
            path = os.path.join(src, name + ".json")
            obs = store.read_json(path, None)
            if isinstance(obs, dict):
                oc = outcomes.get(oid) if isinstance(outcomes.get(oid), dict) else {}
                op = oc.get("op") if oc.get("op") in OUTCOME_OPS else "nothing"
                out = {"op": op, "at": now}
                if op in ("add", "bump", "contradict") and oc.get("item"):
                    out["item"] = str(oc["item"])
                if op == "contradict" and oc.get("new"):
                    out["new"] = str(oc["new"])
                if op == "gap" and oc.get("gap"):
                    out["gap"] = str(oc["gap"])
                obs["outcome"] = out
                try:
                    store.write_json(os.path.join(dst, name + ".json"), obs)
                    os.remove(path)
                    continue
                except OSError:
                    pass
            try:
                os.replace(path, os.path.join(dst, name + ".json"))
            except OSError:
                pass


def due(bot, now=None) -> bool:
    mem = store.manifest(bot)["memory"]
    rows = pending(bot)
    if not rows:
        return False
    if len(rows) >= mem["consolidate_at"]:
        return True
    now = time.time() if now is None else now
    return now - float(rows[0].get("ts") or now) >= mem["consolidate_after_s"]
