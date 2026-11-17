"""Skill-proposal approval cards (docs/experts.md §4 `cards.py`).

A proposal that turned "ready" (`proposals.request`'s thread) is raised as an
ordinary `approval` event when the expert is idle, the way `Bot.greet` runs: a
worker installed as `bot.thread` under `bot.lock`, so `receive()` queues the
user's reply into the inbox and `start_task` refuses meanwhile. Approve /
Reject come back through the existing `/api/bots/<id>/send`. A card is never
raised inside a chat task.

`show` is tried when the proposal turns ready, `tick_one(bot)` when a chat
task of that expert ends, and `tick()` on every scheduler pass. Only "ready"
and "shown" proposals are ever shown ("failed" never is).
"""
from __future__ import annotations

import logging
import threading

from fused_render.bots.channels import base as chan

logger = logging.getLogger(__name__)

_YES = None
_NO = None


def _verdict_res():
    global _YES, _NO
    if _YES is None:
        from fused_render.bots import bot as botmod
        _YES, _NO = botmod._YES, botmod._NO
    return _YES, _NO


def raise_card(bot, proposal) -> int:
    """Emit the approval card for `proposal` and record it as shown. Returns the card's seq. The card's fields:
    `text` (what changes, the lessons, "Approve?"), `detail` = the unified diff, `expert_proposal` = the proposal
    id, `score_before` / `score_after` = always None (kept for compatibility: proposals run no checks)."""
    from fused_render.experts import proposals
    text, detail = proposals.card_text(proposal)
    ev = bot.emit("approval", text, detail=detail, expert_proposal=proposal["id"],
                  score_before=None, score_after=None)
    seq = ev.get("seq")
    try:
        proposals.mark_shown(bot, proposal["id"], seq)
    except Exception:  # noqa: BLE001 — the card is up either way
        logger.warning("expert %s: proposal %s not marked shown", bot.id, proposal.get("id"), exc_info=True)
    bot.set_status("waiting", waiting_on=seq)
    return seq


def _wait_inbox(bot) -> None:
    bot.asking = True
    try:
        while not bot.inbox and not bot.stop_flag.is_set():
            bot.wake.wait(1)
            bot.wake.clear()
    finally:
        bot.asking = False


def wait_verdict(bot, seq, *, keep_live=True, on_other=None):
    """Wait on the inbox for Approve / Reject on card `seq` (agent_engine._gate's loop). Returns
    (verdict, others): True approved, False rejected, None on Stop or (with keep_live False) when a message
    that is neither arrived. `others` are those messages, in order. With keep_live (a chat task) they are
    instructions: `on_other(msg)` hears each, a note says the card is still waiting, and the wait goes on.
    An outside message (a text, an app) is never the verdict unless stamped as the answer to this card."""
    yes, no = _verdict_res()
    others = []
    while True:
        _wait_inbox(bot)
        if bot.stop_flag.is_set():
            return None, others
        verdict, heard = None, False
        for a in bot._drain_inbox():
            mine = not chan.is_texted(a) or chan.answers_card(a, seq)
            if verdict is None and mine and yes.match(str(a)):
                verdict = True
            elif verdict is None and mine and no.match(str(a)):
                verdict = False
            else:
                others.append(a)
                heard = True
                if on_other is not None:
                    on_other(a)
        if verdict is not None:
            return verdict, others
        if heard and not keep_live:
            return None, others
        if heard:
            bot.emit("note", "Noted; still waiting for Approve / Reject on the skill proposal.")
            bot.set_status("waiting", waiting_on=seq)  # receive() flipped it to running


def settle(bot, proposal, verdict) -> tuple[str, dict | None]:
    """Apply the verdict: proposals.approve / reject, a `note` row. Returns (sentence, proposal after)."""
    from fused_render.experts import proposals
    pid = proposal["id"]
    try:
        if verdict:
            p = proposals.approve(bot, pid)
            msg = f"Approved: skill “{p.get('title') or p.get('skill')}” saved."
        else:
            p = proposals.reject(bot, pid)
            msg = f"Rejected: the skill proposal “{p.get('title') or ''}” was dropped; its lessons will not be proposed again."
    except Exception as e:  # noqa: BLE001
        logger.warning("expert %s: proposal %s not settled", bot.id, pid, exc_info=True)
        msg = f"Could not {'save' if verdict else 'reject'} the skill proposal: {e}"
        bot.emit("note", msg)
        return msg, None
    bot.emit("note", msg)
    return msg, p


def show(bot, proposal) -> bool:
    """Raise the approval card for `proposal` when the bot is idle (a worker becomes `bot.thread`); False when it
    is busy (a task or another card), deleted, or the proposal is not open."""
    if not isinstance(proposal, dict) or not proposal.get("id") or proposal.get("status") not in ("ready", "shown"):
        return False
    with bot.lock:
        if getattr(bot, "deleted", False) or getattr(bot, "read_only", False):
            return False
        if bot.thread is not None and bot.thread.is_alive():
            return False
        # stop_flag may still be set from the last Stop (agent_engine.stop sets it even on an idle bot).
        bot.stop_flag.clear()
        bot.pause_flag.clear()
        bot.inbox = []
        t = threading.Thread(target=_worker, args=(bot, proposal), daemon=True, name=f"expert-card-{bot.id}")
        bot.thread = t
        t.start()  # the worker's first emit waits for this lock: nothing runs before the slot is ours
    return True


def _worker(bot, proposal) -> None:
    queued = []
    try:
        bot.set_status("waiting", task="Skill proposal", step=0, note="", waiting_on=None)
        seq = raise_card(bot, proposal)
        verdict, others = wait_verdict(bot, seq, keep_live=False)
        queued = [str(a) for a in others]
        if verdict is not None:
            settle(bot, proposal, verdict)
        elif queued:
            bot.emit("note", "Skill proposal put aside for your message; it comes back when the bot is idle again.")
        else:
            bot.emit("note", "Skill proposal put aside; it comes back later.")
    except Exception as e:  # noqa: BLE001 — never a stuck "waiting"
        logger.warning("expert %s: proposal card failed", bot.id, exc_info=True)
        try:
            bot.emit("error", f"The skill proposal card failed: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            with bot.lock:
                queued += [str(a) for a in bot._drain_inbox()]
                bot.set_status("idle", note="", waiting_on=None)
                if bot.thread is threading.current_thread():
                    bot.thread = None
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: card slot not released cleanly", bot.id, exc_info=True)
            with bot.lock:
                if bot.thread is threading.current_thread():
                    bot.thread = None
        if queued:
            try:
                bot.start_task("\n".join(queued), label=queued[0])
            except Exception:  # noqa: BLE001
                logger.warning("expert %s: queued message not re-delivered", bot.id, exc_info=True)


def _oldest_open(bot):
    from fused_render.experts import proposals
    open_ = [p for p in proposals.listing(bot) if p.get("status") in ("ready", "shown")]
    if not open_:
        return None
    open_.sort(key=lambda p: float(p.get("ts") or 0))
    return open_[0]


def tick_one(bot) -> bool:
    """Show the oldest open proposal (ready, or shown on a card that is no longer up) of this one expert when it
    is idle; with none waiting, ask a gap question that is due (`ask_if_needed`). True when a card went up.
    Never raises."""
    try:
        with bot.lock:
            if bot.thread is not None and bot.thread.is_alive():
                return False
        p = _oldest_open(bot)
        if p is not None:
            return bool(show(bot, p))
        return ask_if_needed(bot)
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: proposal tick failed", getattr(bot, "id", "?"), exc_info=True)
        return False


def tick() -> None:
    """`tick_one` for every expert."""
    try:
        from fused_render.experts import store
        experts = store.all_experts()
    except Exception:  # noqa: BLE001
        return
    for bot in experts:
        tick_one(bot)


# ------------------------------------------------------------------ gap questions ---
GAP_TEXT = ("Sessions asked me this {n} time{s} and I could not find the answer in my skills or the folders I read:"
            "\n\n{q}\n\nIf you tell me, I will remember it. Reply \"skip\" to drop it.")
#: A reply this long or longer is an answer even when it starts like a "no" ("No, it lives in …").
NO_MAX_WORDS = 4


def _busy(bot) -> bool:
    return bot.thread is not None and bot.thread.is_alive()


def ask_if_needed(bot) -> bool:
    """Raise the question card for the gap that is due (`gaps.to_ask`) when the expert is idle and no proposal
    card is waiting. True when a card went up. Never raises."""
    try:
        from fused_render.experts import gaps
        with bot.lock:
            if _busy(bot):
                return False
        if _oldest_open(bot) is not None:
            return False
        g = gaps.to_ask(bot)
        return bool(g is not None and ask_gap(bot, g))
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: gap question tick failed", getattr(bot, "id", "?"), exc_info=True)
        return False


def ask_gap(bot, gap) -> bool:
    """Ask the user about `gap` in the expert's chat when the bot is idle (a worker becomes `bot.thread`, the way
    `show` raises a proposal card). False when it is busy, deleted, or another gap card is up."""
    from fused_render.experts import gaps
    if not isinstance(gap, dict) or not gap.get("id"):
        return False
    with bot.lock:
        if getattr(bot, "deleted", False) or getattr(bot, "read_only", False) or _busy(bot):
            return False
        if not gaps.card_claim(bot, gap["id"]):
            return False
        bot.stop_flag.clear()
        bot.pause_flag.clear()
        bot.inbox = []
        t = threading.Thread(target=_gap_worker, args=(bot, gap), daemon=True, name=f"expert-gap-{bot.id}")
        bot.thread = t
        try:
            t.start()
        except Exception:
            bot.thread = None
            gaps.card_release(bot, gap["id"])
            raise
    return True


def _is_skip(text) -> bool:
    _yes, no = _verdict_res()
    t = str(text or "").strip()
    return t.casefold().strip(" .!") == "skip" or (no.match(t) is not None and len(t.split()) < NO_MAX_WORDS)


def _gap_worker(bot, gap) -> None:
    from fused_render.experts import gaps
    gid = gap["id"]
    queued, settled, stopped = [], False, False
    try:
        bot.set_status("waiting", task="Question from the expert", step=0, note="", waiting_on=None)
        n = int(gap.get("count") or 1)
        ev = bot.emit("question", GAP_TEXT.format(n=n, s="" if n == 1 else "s", q=gap.get("question") or ""),
                      expert_gap=gid, options=["Skip"])
        seq = ev.get("seq")
        gaps.mark_asked(bot, gid, seq)
        bot.set_status("waiting", waiting_on=seq)
        while True:
            _wait_inbox(bot)
            if bot.stop_flag.is_set():
                stopped = True
                break
            reply = None
            for a in bot._drain_inbox():
                # Only the user at this Mac can answer: an answer becomes a lesson the expert trusts.
                if reply is None and not chan.is_texted(a) and str(a).strip():
                    reply = str(a).strip()
                else:
                    queued.append(a)
            if reply is not None:
                if _is_skip(reply):
                    gaps.dismiss(bot, gid)
                    bot.emit("note", "Dropped: I will not ask about this again.")
                else:
                    out = gaps.answer(bot, gid, reply)
                    claim = (out.get("lesson") or {}).get("claim") or reply
                    bot.emit("note", f"Thanks. Saved as a lesson: {claim}")
                settled = True
                break
            if any(chan.is_texted(a) for a in queued):
                bot.emit("note", "Only you, at this Mac, can answer this question; that message waits until it is "
                                 "answered or skipped here.")
            bot.set_status("waiting", waiting_on=seq)  # receive() flipped it to running
    except Exception as e:  # noqa: BLE001 — never a stuck "waiting"
        logger.warning("expert %s: gap question failed", bot.id, exc_info=True)
        try:
            bot.emit("error", f"The question card failed: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        if not settled:
            try:
                gaps.put_aside(bot, gid, stopped=stopped)
            except Exception:  # noqa: BLE001
                logger.warning("expert %s: gap %s not put aside", bot.id, gid, exc_info=True)
        try:
            with bot.lock:
                queued += list(bot._drain_inbox())
                bot.set_status("idle", note="", waiting_on=None)
                if bot.thread is threading.current_thread():
                    bot.thread = None
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: gap card slot not released cleanly", bot.id, exc_info=True)
            with bot.lock:
                if bot.thread is threading.current_thread():
                    bot.thread = None
        gaps.card_release(bot, gid)
        if queued:
            try:
                from fused_render.experts import chat
                bot.start_task("\n".join(str(a) for a in queued), label=str(queued[0]),
                               via=chat._redeliver_via(queued))
            except Exception:  # noqa: BLE001
                logger.warning("expert %s: queued message not re-delivered", bot.id, exc_info=True)
