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


def write(bot, *, kind, claim, trust, session="", about=None, consult="") -> dict:
    obs = {"id": store.new_id("o"), "ts": time.time(), "kind": str(kind or ""),
           "claim": " ".join(str(claim or "").split()), "trust": str(trust or "outcome"),
           "session": str(session or ""), "about": [str(a) for a in (about or []) if str(a).strip()],
           "consult": str(consult or "")}
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


def done(bot) -> list:
    """Archived observations, oldest first (not in the contract; for views)."""
    return _read_dir(_dir(bot, "done"))


def archive(bot, ids) -> None:
    with store.lock(bot):
        src, dst = _dir(bot, "pending"), _dir(bot, "done")
        for oid in ids or []:
            name = _safe(oid)
            if not name:
                continue
            try:
                os.replace(os.path.join(src, name + ".json"), os.path.join(dst, name + ".json"))
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
