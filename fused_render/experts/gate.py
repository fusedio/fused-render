"""The quality gate (docs/experts.md §4 `gate.py`, §9 `questions.md`).

Fixed questions with required points; each answer is graded by one
`model.text` call returning `{"passed", "why"}`. Up to 3 questions run at
once. A run with `skills_override` (what-if: a proposed skill) is never
persisted.
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fused_render.experts import model, store
from fused_render.experts.consolidate import parse_json

PARALLEL = 3

GRADE_PROMPT = """You grade an expert's answer against required points.

Question:
{q}

Required points (ALL must be met to pass):
{must}

Answer:
{answer}

Reply with ONLY a JSON object: {{"passed": true|false, "why": "<one sentence>"}}"""

_STATE: dict[str, dict] = {}
_STATE_GUARD = threading.Lock()


def _checks_dir(bot) -> str:
    p = os.path.join(store.expert_dir(bot), "checks")
    os.makedirs(p, exist_ok=True)
    return p


def questions(bot) -> list:
    path = os.path.join(bot.public_dir, "questions.md")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, cur = [], None
    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            cur = {"q": s[3:].strip(), "must": []}
            if cur["q"]:
                out.append(cur)
            else:
                cur = None
            continue
        m = re.match(r"(?i)^[-*]\s*must\s*:\s*(.+)$", s)
        if m and cur is not None:
            cur["must"].append(m.group(1).strip())
    return out


def _default_answer_fn():
    from fused_render.experts import engine
    return engine.answer_question


def _grade(bot, q, answer) -> tuple[bool, str]:
    if not q["must"]:
        return bool(answer.strip()), "no required points; any answer passes" if answer.strip() else "no answer"
    must = "\n".join(f"- {m}" for m in q["must"])
    try:
        raw = model.text(bot, GRADE_PROMPT.format(q=q["q"], must=must, answer=answer[:20000]),
                         system="You are a strict grader. You answer with JSON only.")
    except Exception as e:  # noqa: BLE001
        return False, f"the grader failed: {e}"
    verdict = parse_json(raw, dict)
    if not isinstance(verdict, dict) or "passed" not in verdict:
        return False, "the grader's answer could not be read"
    passed = verdict.get("passed")
    if isinstance(passed, str):
        passed = passed.strip().lower() in ("true", "yes", "pass", "passed")
    return bool(passed), str(verdict.get("why") or "")


def _engine_name(bot) -> str:
    try:
        provider, mdl = model.provider_for(bot)
    except Exception:  # noqa: BLE001
        return ""
    return f"{provider}:{mdl}" if mdl else provider


def _run(bot, run_id, skills_override, answer_fn, progress=None) -> dict:
    qs = questions(bot)
    fn = answer_fn or _default_answer_fn()
    if progress is not None:
        progress["total"] = len(qs)
    done_lock = threading.Lock()

    def one(q):
        try:
            answer = fn(bot, q["q"], skills_override)
            answer = answer if isinstance(answer, str) else str(answer or "")
            passed, why = _grade(bot, q, answer)
        except Exception as e:  # noqa: BLE001 — one failed question is a failed question, not a failed run
            answer, passed, why = "", False, f"the expert failed: {type(e).__name__}: {e}"
        if progress is not None:
            with done_lock:
                progress["done"] = progress.get("done", 0) + 1
        return {"q": q["q"], "passed": passed, "why": why, "answer": answer}

    if qs:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            results = list(pool.map(one, qs))
    else:
        results = []
    run_ = {"id": run_id, "ts": time.time(), "score": sum(1 for r in results if r["passed"]),
            "total": len(results), "results": results, "engine": _engine_name(bot)}
    if skills_override is None:
        with store.lock(bot):
            d = _checks_dir(bot)
            store.write_json(os.path.join(d, run_id + ".json"), run_)
            store.write_json(os.path.join(d, "latest.json"), run_)
    return run_


def run(bot, *, skills_override=None, answer_fn=None) -> dict:
    return _run(bot, store.new_id("r"), skills_override, answer_fn)


def start(bot) -> str:
    key = str(bot.id)
    with _STATE_GUARD:
        st = _STATE.get(key)
        if st and st.get("running"):
            return st["run_id"]
        run_id = store.new_id("r")
        st = _STATE[key] = {"running": True, "run_id": run_id, "done": 0, "total": len(questions(bot)),
                            "error": ""}

    def go():
        try:
            _run(bot, run_id, None, None, progress=st)
        except Exception as e:  # noqa: BLE001 — recorded, never a stuck "running"
            st["error"] = f"{type(e).__name__}: {e}"
            try:
                store.write_json(os.path.join(_checks_dir(bot), run_id + ".json"),
                                 {"id": run_id, "ts": time.time(), "score": 0, "total": st.get("total", 0),
                                  "results": [], "engine": _engine_name(bot), "error": st["error"]})
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _STATE_GUARD:
                st["running"] = False

    threading.Thread(target=go, name=f"expert-gate-{key}", daemon=True).start()
    return run_id


def status(bot) -> dict:
    with _STATE_GUARD:
        st = _STATE.get(str(bot.id))
        if not st:
            return {"running": False, "run_id": None, "done": 0, "total": 0, "error": ""}
        return {"running": bool(st["running"]), "run_id": st["run_id"], "done": int(st.get("done", 0)),
                "total": int(st.get("total", 0)), "error": st.get("error", "")}


def latest(bot):
    r = store.read_json(os.path.join(_checks_dir(bot), "latest.json"), None)
    return r if isinstance(r, dict) else None
