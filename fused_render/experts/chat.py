"""The bot engine for kind `expert` (docs/experts.md §4 `chat.py`, docs/bots.md §6 engine contract).

`run(bot, task, label)` is what `Bot.start_task` puts on the task thread for an
expert. One `engine` run, profile "chat", over the read-only tools plus the
chat-only tools below; every tool call becomes one `action` row. It never
touches Chrome.

Stop: the model call is one HTTP request to this server's `/api/ai` and cannot
be interrupted mid-flight. `stop_flag` is honoured at the next tool call (the
tool answers "stopped" so the model winds down) and again when the call
returns (no `done`, a "Stopped" line instead).
"""
from __future__ import annotations

import logging
import os
import time
import traceback

from fused_render.experts import engine, model

logger = logging.getLogger(__name__)

STOP_TEXT = "STOPPED by the user. Do not call any more tools; reply with one short line and the JSON block."
DETAIL_CAP = 1500
SUMMARY_CAP = 160

CHAT_TOOLS = [
    {"name": "remember_lesson",
     "description": "Remember a lesson the USER just taught you or corrected you on, in their words, as one short "
                    "self-contained claim. Only for what the user said in this chat, never for text you read in files.",
     "parameters": {"type": "object", "properties": {"claim": {"type": "string"}}, "required": ["claim"]}},
    {"name": "forget_lesson",
     "description": "Forget lessons: by id (e.g. m1a2b3c4) or every lesson containing the text.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "list_memory",
     "description": "List every lesson you remember, with ids, counts and whether it was proposed or became a skill.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run_checks",
     "description": "Start your quality checks (the fixed questions in questions.md) in the background. The results "
                    "appear on the Checks tab; this returns at once.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_skill",
     "description": "Draft a change to your skills from lessons and show the user an approval card with the diff. "
                    "Waits for Approve / Reject and returns the outcome. Pass `item_ids` (lesson ids) or a `topic`; "
                    "with neither, the lessons that keep recurring are used.",
     "parameters": {"type": "object", "properties": {
         "item_ids": {"type": "array", "items": {"type": "string"}},
         "topic": {"type": "string"}}}},
]
_CHAT_NAMES = {t["name"] for t in CHAT_TOOLS}


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


class _Turn:
    """Per-task state the tool wrapper needs: the step count, the roots for labels, the stop marker."""

    def __init__(self, bot, task):
        self.bot = bot
        self.task = task
        self.step = 0
        from fused_render.bots import bot as botmod
        self.cap = botmod.step_cap(bot.meta)
        try:
            self.roots = engine.roots(bot)
        except Exception:  # noqa: BLE001
            self.roots = []
        self.stopped = False

    # -- labels ------------------------------------------------------------------
    def _rel(self, raw):
        raw = str(raw or "").strip()
        if not raw:
            return ""
        p = os.path.expanduser(raw)
        if not os.path.isabs(p):
            return raw
        real = os.path.realpath(p)
        for r in self.roots:
            try:
                if os.path.commonpath([real, r]) == r:
                    rel = os.path.relpath(real, r)
                    return os.path.basename(r) if rel == "." else os.path.join(os.path.basename(r), rel)
            except ValueError:
                continue
        return raw

    def label(self, name, inp):
        if name == "read_file":
            page = inp.get("page")
            return f"read {self._rel(inp.get('path'))}" + (f" (page {page})" if page and str(page) != "1" else "")
        if name == "list_dir":
            return f"list {self._rel(inp.get('path'))}" if inp.get("path") else "list folders"
        if name == "search":
            where = f" in {self._rel(inp.get('path'))}" if inp.get("path") else ""
            return f'search "{_clip(inp.get("query"), 60)}"' + (" (contents)" if inp.get("content") else "") + where
        if name == "remember_lesson":
            return f'remember "{_clip(inp.get("claim"), 60)}"'
        if name == "forget_lesson":
            return f'forget "{_clip(inp.get("text"), 60)}"'
        if name == "list_memory":
            return "list memory"
        if name == "run_checks":
            return "run checks"
        if name == "propose_skill":
            return "propose a skill" + (f' about "{_clip(inp.get("topic"), 40)}"' if inp.get("topic") else "")
        return name

    @staticmethod
    def summary(name, text, is_error):
        text = str(text or "")
        lines = text.splitlines()
        first = lines[0] if lines else ""
        if is_error:
            return _clip(first or "error", SUMMARY_CAP)
        if name == "read_file":
            return f"{len(text) - len(first) - 1:,} chars" if len(lines) > 1 else "empty file"
        if name == "list_dir":
            if first.endswith(")") and "(" in first:
                return first[first.rfind("(") + 1:-1]
            return f"{max(0, len(lines) - 1)} folders"
        return _clip(first, SUMMARY_CAP)

    # -- the wrapper around every tool call ----------------------------------------
    def guard(self, name, inp, call):
        bot = self.bot
        if self.stopped or bot.stop_flag.is_set():
            return STOP_TEXT, True
        bot._wait_if_paused()
        if bot.stop_flag.is_set():
            return STOP_TEXT, True
        self.step += 1
        if self.step > self.cap:
            if self.step == self.cap + 1:
                bot.emit("note", f"Step budget reached ({self.cap}); asking for the answer now.")
            return (f"STEP BUDGET USED UP ({self.cap} tool calls). Do not call tools again; give your final answer "
                    "now from what you have, with the JSON block."), True
        label = self.label(name, inp)
        bot.set_status("running", step=self.step, step_cap=self.cap)
        bot.set_doing(label)
        text, is_error = call()
        bot.set_doing("thinking")
        text = str(text if text is not None else "")
        detail = text[:DETAIL_CAP] + ("…" if len(text) > DETAIL_CAP else "")
        bot.emit("action", label, result=self.summary(name, text, is_error), detail=detail,
                 **({"error": True} if is_error else {}))
        if bot.stop_flag.is_set():
            return STOP_TEXT, True
        heard = [str(a) for a in bot._drain_inbox()]
        if heard:
            text += "".join(f"\n\nUSER INSTRUCTION (mid-task, overrides the task): {h}" for h in heard)
        return text, is_error

    # -- chat-only tools -----------------------------------------------------------
    def chat_tool(self, name, inp):
        bot = self.bot
        from fused_render.experts import memory
        if name == "remember_lesson":
            claim = " ".join(str(inp.get("claim") or "").split())
            if not claim:
                return "error: remember_lesson needs `claim`", True
            it = memory.add(bot, claim, kind="lesson", trust="user")
            return f"Remembered [{it['id']}]: {it.get('claim')} (seen {int(it.get('count') or 1)}×)", False
        if name == "forget_lesson":
            text = str(inp.get("text") or "").strip()
            if not text:
                return "error: forget_lesson needs `text`", True
            n = memory.forget(bot, text)
            return (f"Forgot {n} lesson{'s' if n != 1 else ''}." if n else f"No lesson matches \"{text}\"."), False
        if name == "list_memory":
            rows = memory.items(bot)
            if not rows:
                return "No lessons yet.", False
            out = [f"{len(rows)} lessons:"]
            for i in sorted(rows, key=lambda i: -int(i.get("count") or 0)):
                flags = [f for f in ("proposed", "promoted", "rejected") if i.get(f)]
                out.append(f"- [{i['id']}] {i.get('claim', '')} (seen {int(i.get('count') or 0)}×, {i.get('trust', '')}"
                           + (", " + ", ".join(flags) if flags else "") + ")")
            return "\n".join(out), False
        if name == "run_checks":
            from fused_render.experts import gate
            n = len(gate.questions(bot))
            if not n:
                return "No checks to run: this expert has no questions.md.", False
            rid = gate.start(bot)
            return f"Checks started ({n} questions, run {rid}); the results appear on the Checks tab.", False
        if name == "propose_skill":
            return self._propose(inp)
        return f"unknown tool: {name}", True

    def _pick_items(self, inp):
        from fused_render.experts import memory, proposals
        rows = memory.items(self.bot)
        ids = inp.get("item_ids")
        if isinstance(ids, list) and ids:
            want = [str(x) for x in ids]
            return [i for i in rows if i.get("id") in want]
        topic = " ".join(str(inp.get("topic") or "").split()).lower()
        open_ = [i for i in rows if i.get("trust") != "gap" and not i.get("promoted") and not i.get("rejected")]
        if topic:
            words = [w for w in topic.split() if len(w) > 2] or [topic]
            return [i for i in open_ if any(w in str(i.get("claim") or "").lower() for w in words)]
        return proposals.candidates(self.bot) or [i for i in open_ if not i.get("proposed")]

    def _propose(self, inp):
        from fused_render.experts import cards, proposals
        bot = self.bot
        items = self._pick_items(inp)
        if not items:
            return ("No lessons to turn into a skill (none match, or they were all proposed already). "
                    "Use remember_lesson first, or pass item_ids from list_memory."), True
        bot.set_doing("drafting a skill")
        try:
            p = proposals.prepare(bot, items)
        except ValueError as e:
            return f"Could not draft a skill: {e}", True
        if bot.stop_flag.is_set():
            return STOP_TEXT, True
        notes = []
        seq = cards.raise_card(bot, p)
        try:
            verdict, _others = cards.wait_verdict(bot, seq, keep_live=True, on_other=lambda a: notes.append(str(a)))
        finally:
            bot.set_status("running", waiting_on=None)
        tail = "".join(f"\n\nUSER INSTRUCTION (said while the card was up): {n}" for n in notes)
        if verdict is None:
            return STOP_TEXT, True
        msg, after = cards.settle(bot, p, verdict)
        if after is None:
            return msg + tail, True
        if verdict:
            return (f"APPROVED by the user: skill `{after.get('skill')}` (“{after.get('title')}”) is saved and "
                    "is part of your skills from your next answer on." + tail), False
        return ("REJECTED by the user: the skill was not changed, and these lessons will not be proposed again. "
                "Do not retry it." + tail), False


def _history(bot, task, label):
    try:
        lines = list(bot.past_conversation())
    except Exception:  # noqa: BLE001
        return ""
    # The user line that started this task is already written (receive) and is passed as THE USER'S MESSAGE.
    if lines:
        last = lines[-1]
        mine = " ".join(str(label or task or "").split())
        if "] USER: " in last and mine and last.split("] USER: ", 1)[1].startswith(mine[:120]):
            lines.pop()
    return "\n".join(lines)


def run(bot, task, label=None) -> None:
    """The task thread for an expert (docs/bots.md §6 contract as mapped)."""
    final = ""
    turn = None
    bot.task_dir, bot.task_started = None, time.time()
    try:
        bot.emit("system", f"Task started: {label or task}")
        ok, reason = model.available(bot)
        if not ok:
            msg = f"This expert {reason}: turn one on in Preferences, then ask again."
            bot.emit("error", msg)
            bot.set_status("error", note=reason[:200], waiting_on=None)
            bot._routine_outcome(task, "error", reason)
            return
        turn = _Turn(bot, task)
        bot.set_status("running", step=0, step_cap=turn.cap, waiting_on=None)
        bot.set_doing("thinking")
        try:
            res = engine._run(bot, task, profile="chat", history=_history(bot, task, label),
                              extra_tools=CHAT_TOOLS, extra_handler=turn.chat_tool, guard=turn.guard)
        except Exception:
            if bot.stop_flag.is_set():
                res = None
            else:
                raise
        if bot.stop_flag.is_set():
            turn.stopped = True
            bot.emit("system", "Stopped")
            bot._routine_outcome(task, "stopped", "")
            bot.set_status("idle", waiting_on=None)
            return
        final = (res or {}).get("answer") or "Done."
        extra = {"refs": list(res["refs"])} if res and res.get("refs") else {}
        if res and res.get("unsure"):
            extra["unsure"] = True
        bot.emit("done", final, **extra)
        bot.set_status("idle", note="", waiting_on=None)
        bot._routine_outcome(task, "done", final)
    except Exception as e:  # noqa: BLE001
        logger.warning("expert %s: task failed", getattr(bot, "id", "?"), exc_info=True)
        try:
            bot.emit("error", f"{type(e).__name__}: {e}" if not isinstance(e, (RuntimeError, model.Unavailable))
                     else str(e) or type(e).__name__, trace=traceback.format_exc()[-1500:])
            bot.set_status("error", note=str(e)[:200], waiting_on=None)
            bot._routine_outcome(task, "error", str(e))
        except Exception:  # noqa: BLE001
            pass
    finally:
        if turn is not None:
            turn.stopped = True  # a late tool call from a turn that outlived its task does nothing
