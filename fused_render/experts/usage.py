"""What an expert's model calls cost (docs/experts.md §4 `usage.py`).

Every call `model.text` / `model.tool_turn` makes is one line of `<expert_dir>/usage.jsonl`:
`{ts, stage, provider, model, input_tokens, output_tokens, cache_read_tokens, rounds, ms, ok, estimated}`.
Token counts are the gateway's when its `done` result carries `usage`; otherwise they are estimated as
characters / 4 and the line says `estimated: true`. Writing a line never fails the call it describes.

`stage` says what the call was for. The engine sets it for its runs (`stage(...)` scope); a module that calls
`model.text` itself is labelled by its own name (`infer_stage`).
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
import time

from fused_render.experts import store

logger = logging.getLogger(__name__)

USAGE_FILE = "usage.jsonl"
STAGES = ("consult", "chat", "research", "checks_answer", "checks_grade", "capture", "consolidate", "tidy",
          "draft", "other")
#: The calling module (last name part) -> its stage, for callers that name none.
MODULE_STAGES = {"capture": "capture", "consolidate": "consolidate", "tidy": "tidy", "proposals": "draft",
                 "gate": "checks_grade", "gaps": "research"}
_SKIP_MODULES = frozenset({"fused_render.experts.model", "fused_render.experts.usage"})
CHARS_PER_TOKEN = 4

_STAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar("fused_render_expert_stage", default=None)


@contextlib.contextmanager
def stage(name):
    """Calls made inside this block (this thread / context) are labelled `name` unless they name a stage."""
    token = _STAGE.set(name if name in STAGES else "other")
    try:
        yield
    finally:
        _STAGE.reset(token)


def current_stage() -> str | None:
    return _STAGE.get()


def infer_stage() -> str:
    """The stage of the first caller outside model.py / usage.py, by its module name; "other" when unknown."""
    try:
        f = sys._getframe(1)
    except ValueError:
        return "other"
    while f is not None:
        mod = f.f_globals.get("__name__") or ""
        if mod not in _SKIP_MODULES and not mod.startswith("contextlib"):
            return MODULE_STAGES.get(mod.rsplit(".", 1)[-1], "other")
        f = f.f_back
    return "other"


def resolve_stage(explicit=None) -> str:
    """An explicit stage, else the engine's scope, else the caller's module."""
    if explicit:
        return explicit if explicit in STAGES else "other"
    return _STAGE.get() or infer_stage()


def estimate(chars) -> int:
    try:
        return max(0, int(chars) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
    except (TypeError, ValueError):
        return 0


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def tokens_from_result(result) -> dict | None:
    """`{input_tokens, output_tokens, cache_read_tokens, rounds}` from a gateway result frame, or None when it
    reports no token counts. Reads the wire's camelCase `usage` (`inputTokens`, `outputTokens`) and the CLI's own
    names should they ever ride along (`input_tokens`, `cache_read_input_tokens`, `num_turns`)."""
    if not isinstance(result, dict):
        return None
    u = result.get("usage")
    if not isinstance(u, dict):
        return None
    inp = _int(u.get("inputTokens", u.get("input_tokens")))
    out = _int(u.get("outputTokens", u.get("output_tokens")))
    if inp is None and out is None:
        return None
    cache = None
    for k in ("cacheReadTokens", "cachedInputTokens", "cache_read_input_tokens", "cache_read_tokens"):
        cache = _int(u.get(k))
        if cache is not None:
            break
    rounds = None
    meta = result.get("providerMetadata")
    for src in [result, u] + ([v for v in meta.values() if isinstance(v, dict)] if isinstance(meta, dict) else []):
        for k in ("numTurns", "num_turns"):
            rounds = _int(src.get(k))
            if rounds is not None:
                break
        if rounds is not None:
            break
    return {"input_tokens": inp or 0, "output_tokens": out or 0, "cache_read_tokens": cache, "rounds": rounds}


def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), USAGE_FILE)


def record(bot, *, stage, provider, model, input_tokens, output_tokens, cache_read_tokens=None, rounds=1, ms=0,
           ok=True, estimated=False) -> dict | None:
    """Append one usage line. Never raises; returns the line (None when it could not be written)."""
    try:
        line = {"ts": time.time(), "stage": stage if stage in STAGES else "other", "provider": str(provider or ""),
                "model": str(model or ""), "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "cache_read_tokens": None if cache_read_tokens is None else int(cache_read_tokens),
                "rounds": int(rounds or 0), "ms": int(ms or 0), "ok": bool(ok), "estimated": bool(estimated)}
        store.append_jsonl(_path(bot), line)
        return line
    except Exception:  # noqa: BLE001 — accounting never breaks a model call
        logger.warning("expert %s: usage line not written", getattr(bot, "id", "?"), exc_info=True)
        return None


def lines(bot) -> list:
    try:
        return store.read_jsonl(_path(bot))
    except Exception:  # noqa: BLE001
        return []


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def summary(bot, days=7, now=None) -> dict:
    """Tokens per stage and per day over the last `days` days (local calendar days, today included).
    `estimated_share` is the share of a stage's calls whose counts are estimates (0..1)."""
    try:
        days = max(1, min(int(days), 366))
    except (TypeError, ValueError):
        days = 7
    now = time.time() if now is None else float(now)
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))
    by_stage: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "estimated_share": 0.0}
    est_total = 0
    for ln in lines(bot):
        if not isinstance(ln, dict):
            continue
        try:
            ts = float(ln.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start or ts > now + 86400:
            continue
        st = ln.get("stage") if ln.get("stage") in STAGES else "other"
        i = _int(ln.get("input_tokens")) or 0
        o = _int(ln.get("output_tokens")) or 0
        c = _int(ln.get("cache_read_tokens")) or 0
        est = bool(ln.get("estimated"))
        row = by_stage.setdefault(st, {"stage": st, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                       "cache_read_tokens": 0, "_est": 0})
        row["calls"] += 1
        row["input_tokens"] += i
        row["output_tokens"] += o
        row["cache_read_tokens"] += c
        row["_est"] += int(est)
        d = _day(ts)
        drow = by_day.setdefault(d, {"day": d, "calls": 0, "input_tokens": 0, "output_tokens": 0})
        drow["calls"] += 1
        drow["input_tokens"] += i
        drow["output_tokens"] += o
        total["calls"] += 1
        total["input_tokens"] += i
        total["output_tokens"] += o
        total["cache_read_tokens"] += c
        est_total += int(est)
    stages = []
    for row in by_stage.values():
        n = row.pop("_est")
        row["estimated_share"] = round(n / row["calls"], 3) if row["calls"] else 0.0
        stages.append(row)
    stages.sort(key=lambda r: (-(r["input_tokens"] + r["output_tokens"]), STAGES.index(r["stage"])))
    total["estimated_share"] = round(est_total / total["calls"], 3) if total["calls"] else 0.0
    return {"days": days, "by_stage": stages, "by_day": sorted(by_day.values(), key=lambda r: r["day"]),
            "total": total}
