"""Files, locks and lookup for experts (docs/experts.md §2-§4).

Every expert's private state lives under `<bot.dir>/expert/`. JSON writes are
atomic (temp file + `os.replace`); a read-modify-write of one expert's files
holds `lock(bot)`, one `threading.RLock` per bot id.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid

KIND = "expert"

#: The manifest's defaults (§3). `manifest(bot)` fills every missing key.
DEFAULT_MANIFEST = {
    "ask_me_about": "",
    "reads": [],
    "offered_to": {"targets": []},
    "learns_from": ["repeated_request", "user_correction", "error_then_fix",
                    "answer_contradicted", "unanswered"],
    "engine": "auto",
    "model": "",
    # Whether the expert learns in the background (docs/experts.md). Off: sessions are still noticed.
    "learning": True,
    "memory": {"consolidate_at": 3, "consolidate_after_s": 86400, "propose_at": 3,
               "index_lines": 200,
               # The memory budget (docs/experts.md "Memory budget"): at most `limit` lessons; above `tidy_at`
               # consolidation also tidies (deletes stale, rarely used lessons; promotes heavily used ones).
               "limit": 50, "tidy_at": 30, "stale_days": 14, "rare_uses": 2, "heavy_uses": 5},
    "shipped": "",
    "gaps": {"research": True, "max_attempts": 2},
}
ENGINE_CHOICES = ("auto", "claude", "codex", "copilot")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _meta_of(meta_or_bot) -> dict:
    if isinstance(meta_or_bot, dict):
        return meta_or_bot
    meta = getattr(meta_or_bot, "meta", None)
    return meta if isinstance(meta, dict) else {}


def is_expert(meta_or_bot) -> bool:
    return _meta_of(meta_or_bot).get("kind") == KIND


def _int(v, default, lo=0):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def manifest(bot) -> dict:
    """The expert's manifest with every default filled in (a deep copy: editing
    the result never edits `bot.meta`)."""
    raw = _meta_of(bot).get("expert")
    raw = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(DEFAULT_MANIFEST)
    if isinstance(raw.get("ask_me_about"), str):
        out["ask_me_about"] = raw["ask_me_about"]
    if isinstance(raw.get("reads"), list):
        out["reads"] = [copy.deepcopy(r) for r in raw["reads"] if isinstance(r, dict)]
    offered = raw.get("offered_to")
    if isinstance(offered, dict) and isinstance(offered.get("targets"), list):
        out["offered_to"] = {**copy.deepcopy(offered),
                             "targets": [str(t) for t in offered["targets"] if isinstance(t, str)]}
    if isinstance(raw.get("learns_from"), list):
        out["learns_from"] = [str(k) for k in raw["learns_from"] if isinstance(k, str)]
    engine = raw.get("engine")
    if engine in ENGINE_CHOICES:
        out["engine"] = engine
    if isinstance(raw.get("model"), str):
        out["model"] = raw["model"].strip()
    if isinstance(raw.get("learning"), bool):
        out["learning"] = raw["learning"]
    mem = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    d = DEFAULT_MANIFEST["memory"]
    out["memory"] = {
        "consolidate_at": _int(mem.get("consolidate_at"), d["consolidate_at"], 1),
        "consolidate_after_s": _int(mem.get("consolidate_after_s"), d["consolidate_after_s"], 0),
        "propose_at": _int(mem.get("propose_at"), d["propose_at"], 1),
        "index_lines": _int(mem.get("index_lines"), d["index_lines"], 1),
        "limit": _int(mem.get("limit"), d["limit"], 1),
        "tidy_at": _int(mem.get("tidy_at"), d["tidy_at"], 0),
        "stale_days": _int(mem.get("stale_days"), d["stale_days"], 0),
        "rare_uses": _int(mem.get("rare_uses"), d["rare_uses"], 0),
        "heavy_uses": _int(mem.get("heavy_uses"), d["heavy_uses"], 1),
    }
    gaps = raw.get("gaps") if isinstance(raw.get("gaps"), dict) else {}
    g = DEFAULT_MANIFEST["gaps"]
    out["gaps"] = {
        "research": gaps["research"] if isinstance(gaps.get("research"), bool) else g["research"],
        "max_attempts": _int(gaps.get("max_attempts"), g["max_attempts"], 1),
    }
    if isinstance(raw.get("shipped"), str):
        out["shipped"] = raw["shipped"]
    # Keys this module does not know ride along untouched.
    for k, v in raw.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
    return out


def expert_dir(bot) -> str:
    p = os.path.join(bot.dir, "expert")
    os.makedirs(p, exist_ok=True)
    return p


def lock(bot) -> threading.RLock:
    bid = str(getattr(bot, "id", bot))
    with _LOCKS_GUARD:
        lk = _LOCKS.get(bid)
        if lk is None:
            lk = _LOCKS[bid] = threading.RLock()
        return lk


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def write_json(path, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


_JSONL_GUARD = threading.Lock()


def append_jsonl(path, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _JSONL_GUARD:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def read_jsonl(path, limit=None) -> list:
    """Every parsable line, oldest first; `limit` keeps the newest N."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if limit is not None:
        limit = max(0, int(limit))
        out = out[-limit:] if limit else []
    return out


def all_experts() -> list:
    """Every bot of kind expert. Never raises (an unreachable registry is no experts)."""
    try:
        from fused_render.bots import registry
        bots = registry.all()
    except Exception:  # noqa: BLE001
        return []
    return [b for b in bots if is_expert(b) and not getattr(b, "deleted", False)]


def find(id_or_name):
    """By id, else exact name (case-insensitive), else the manifest's `shipped` key."""
    key = str(id_or_name or "").strip()
    if not key:
        return None
    experts = all_experts()
    for b in experts:
        if b.id == key:
            return b
    low = key.casefold()
    for b in experts:
        if str(b.meta.get("name") or "").strip().casefold() == low:
            return b
    for b in experts:
        shipped = manifest(b).get("shipped") or ""
        if shipped and shipped.casefold() == low:
            return b
    return None


def new_id(prefix) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"
