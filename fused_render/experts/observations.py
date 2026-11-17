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


def write(bot, *, kind, claim, trust, session="", about=None, consult="", source=None) -> dict:
    """`source`: where it came from when known, `{"target": str, "backend": str}` (the consulting or captured
    session's folder and engine)."""
    obs = {"id": store.new_id("o"), "ts": time.time(), "kind": str(kind or ""),
           "claim": " ".join(str(claim or "").split()), "trust": str(trust or "outcome"),
           "session": str(session or ""), "about": [str(a) for a in (about or []) if str(a).strip()],
           "consult": str(consult or "")}
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


OUTCOME_OPS = ("add", "bump", "nothing", "gap")


def archive(bot, ids, outcomes=None) -> None:
    """pending/ -> done/. `outcomes` maps an observation id to what consolidation did with it:
    `{"op": "add"|"bump"|"nothing"|"gap", "item"?: memory item id, "gap"?: gap id}`; it is stored on the archived
    record as `outcome` (with `at`). An id without one gets `{"op": "nothing"}`."""
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
                if op in ("add", "bump") and oc.get("item"):
                    out["item"] = str(oc["item"])
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
