"""Self-fix — the HTTP surface over `fused_render/selffix.py` (SPEC §42).

Three routes, and the split between them is what each costs:

  POST /api/selffix/start   start a Claude session on this installation
  GET  /api/selffix         everything the modified-version panel shows
  POST /api/selffix/clear   forget the modification (the user's own override)

One session at a time (`_claim_active`): they all edit the same tree, so a
second one is not concurrency, it is a conflict.

The BADGE itself is not here: whether the chip is amber rides on /api/config's
`modified_install` field, because the shell already polls that endpoint on every
route and a second poll for one boolean would be a second poll for one boolean.
What this endpoint answers is the panel's contents — the report list, the
reinstall instructions — which cost a directory walk and (on a mac) a `brew`
subprocess, and are read once, when the user opens the panel.

Reads are unguarded and writes carry the X-Fused header, exactly as everywhere
else.
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Body, Header

from fused_render import claude_spawn, selffix
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# How often the watcher re-hashes the installation while a session runs, in
# poll ticks (claude_spawn polls every 2s, so ~16s).
#
# There IS a reason to check before the turn ends, and it is not impatience: the
# user is watching this session in the sidebar, and the badge appearing while
# they watch is what tells them the app noticed. The digest is a walk of the
# package (~460 files), so it is not something to do on every tick — but once
# every few seconds of a session someone is sitting in front of is nothing.
_CHECK_EVERY_TICKS = 8

# ONE fix session at a time, because they all edit the same tree. Two agents
# rewriting one installation concurrently is not a slow path, it is a conflict:
# each reads a file the other is midway through changing, and the report each
# writes describes a state that never existed. A user with two failed rows
# clicking Fix on both is the ordinary way to get there.
#
# In memory, like the job registry: it describes work happening in THIS process,
# and a restart is the end of it. Held past the run's end by nothing — the
# watcher releases it in a `finally` — with a TTL as the backstop for a watcher
# thread that died without running one.
_ACTIVE_TTL_S = 3600.0
_active_lock = threading.Lock()
_active: dict | None = None


def _claim_active(now: float) -> str | None:
    """Take the slot, or say which run already has it."""
    global _active
    with _active_lock:
        if _active is not None and (now - _active["at"]) < _ACTIVE_TTL_S:
            return str(_active["run_id"])
        _active = {"run_id": "", "at": now}
        return None


def _set_active_run(run_id: str) -> None:
    global _active
    with _active_lock:
        if _active is not None:
            _active["run_id"] = run_id


def _release_active() -> None:
    global _active
    with _active_lock:
        _active = None


# Re-bound as module-level names (the apps router's convention) so a test can
# swap the spawn without reaching into another module.
_spawn_helper = claude_spawn.spawn_helper
_load_agent = claude_spawn.load_agent
_record_session_when_ready = claude_spawn.record_session_when_ready


def _watch_fix(run_id: str, incident: str, report: str, title: str,
               before: str) -> None:
    """Follow the fix session and stamp the installation if it changed.

    Two jobs the session cannot do for itself. The FIRST is the stamp — see
    `selffix.settle` for why the app decides that rather than the model, and why
    it is measured against `before` (the tree as THIS session found it) rather
    than against the release. The SECOND is the one every detached session in
    this codebase needs: nobody polls a run started from an HTTP request, and a
    run nobody polls never reaches its sidecar, so the conversation would not be
    listed when the user later opens that folder's chat.

    Every failure is swallowed. A fix that landed but whose badge did not is a
    bad outcome; a background thread that raises into the server is a worse one.
    """
    state = {"ticks": 0, "session_id": ""}

    def stamp() -> None:
        try:
            selffix.settle(before=before, run_id=run_id,
                           session_id=state["session_id"],
                           report=report, incident=incident, title=title)
        except Exception:  # noqa: BLE001 — bookkeeping, never fatal
            logger.debug("self-fix stamp failed", exc_info=True)

    def on_tick(data: dict) -> bool:
        session_id = str(data.get("session_id") or "")
        if session_id:
            state["session_id"] = session_id
        state["ticks"] += 1
        # The `done` tick is checked first and unconditionally: it is the only
        # one guaranteed to happen, and a session that edits one file in its
        # last thirty seconds must still be caught.
        if data.get("done") or state["ticks"] % _CHECK_EVERY_TICKS == 0:
            stamp()
        return True

    try:
        _record_session_when_ready(_load_agent(), run_id, on_tick=on_tick)
    except Exception:  # noqa: BLE001
        logger.debug("self-fix watcher failed", exc_info=True)
    finally:
        # One last look regardless of how the poll ended — it gives up after an
        # hour (claude_spawn._RECORD_POLL_TICKS), and a session that ran long is
        # exactly the one most likely to have changed something. In a `finally`
        # so a watcher that died still frees the slot: the alternative is an app
        # that refuses every later fix for an hour because of one bad thread.
        stamp()
        _release_active()


@router.post("/api/selffix/start")
def api_selffix_start(body: dict = Body(default={}),
                      x_fused: str | None = Header(default=None)):
    """Open a Claude Code session on this installation, about one failure.

    TWO WAYS IN, one endpoint:

      a failed row     `{job_id, title, detail, state, kind, message, page}` —
                       the download manager's own record of what broke.
      a description    `{note}` — the Preferences tab, where the user is
                       reporting behaviour rather than a crash. No traceback
                       exists, so the incident carries the app log instead and
                       the prompt tells the session to reproduce before it
                       diagnoses.

    One of `message`, `note` or `title` is required. Not as validation for its
    own sake: a session handed nothing at all has no failure, no description and
    no name for what it is looking at — it would read code at random and then
    write a report about having done so, which is worse than the button not
    working, because it costs the user minutes to find that out.

    Answers `{run_id, target, incident, report}`. The shell builds the sidebar
    URL from `target` + `run_id` (platform/lib/selffix.ts) rather than being
    handed one: view-vs-embed prefix is a fact about the document, and the
    server has no business guessing which one is asking.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body, dict):
        return _error("request body must be a JSON object")
    # WHICH BRIEF the session gets, and it turns on whether the USER DESCRIBED
    # this — not on whether an error text came with it. A failed job row is
    # allowed to carry an empty `message` (jobs.py leaves it ""; the manager
    # renders a bare "Failed"), and keying off the message would hand that row
    # the Preferences brief: "NOTHING CRASHED, the user opened Preferences and
    # described something" — false on both counts, and it steers the session
    # away from tracing a failure that really happened. A `note` is the one
    # thing only the describe path produces.
    described = bool(str(body.get("note") or "").strip())
    reported_error = not described
    if not (described or str(body.get("message") or "").strip()
            or str(body.get("title") or "").strip()):
        return _error(
            "say what is wrong — a fix session needs either a failure to read "
            "or a description of what the app is doing wrong")

    root = selffix.install_root()
    if not selffix.writable():
        # Refused BEFORE the spawn, not reported after it. A session that cannot
        # write is several minutes of reading followed by a report describing a
        # fix that was never applied — which reads, to the user watching, like a
        # fix that WAS applied.
        return _error(
            f"this installation is read-only ({root}) — a local fix cannot be "
            "applied here. Reinstall fused-render, or install it somewhere you "
            "own.", status=409)

    if (busy := _claim_active(time.time())) is not None:
        return _error(
            "a fix session is already running on this installation"
            + (f" (run {busy})" if busy else "")
            + ". Finish or stop it before starting another — two sessions "
            "editing the same files at once leave neither report true.",
            status=409)

    try:
        incident, report = selffix.record_incident(body)
        # BEFORE the session starts and never after. Two digests, not one: the
        # release's (for `reconcile`) and the tree as this session finds it
        # (what `settle` measures against) — see `selffix.begin_session`. The
        # incident write above cannot disturb either; the state dir is outside
        # the digest.
        _, before = selffix.begin_session()
    except OSError as exc:
        _release_active()
        return _error(f"could not write the incident file: {exc}", status=500)

    try:
        res = _spawn_helper(
            root, selffix.fix_prompt(incident, report, reported_error=reported_error),
            selffix.FIX_PERMISSION_MODE)
    except Exception as exc:  # noqa: BLE001 — the reason belongs in the response
        _release_active()
        return _error(f"could not start the fix session: {exc}", status=502)
    run_id = res.get("run_id")
    if res.get("error") or not run_id:
        _release_active()
        return _error(str(res.get("error") or "could not start the fix session"),
                      status=502)

    _set_active_run(str(run_id))
    # The marker's label for this fix. A described problem has no operation
    # name, so its first line stands in — the panel lists fixes by this, and
    # "a problem the user described" over every row says nothing.
    title = str(body.get("title") or "").strip()
    if not title:
        first_line = str(body.get("note") or "").strip().splitlines()
        title = first_line[0][:120] if first_line else ""
    try:
        threading.Thread(target=_watch_fix,
                         args=(str(run_id), incident, report, title, before),
                         daemon=True, name="fused-render-selffix-watch").start()
    except Exception:  # noqa: BLE001 — the session is already running
        # Nothing will stamp and nothing will free the slot, so do both now: a
        # session that ran with no watcher is exactly the case where the mark
        # matters, and the TTL alone would lock the feature out for an hour.
        logger.exception("could not start the self-fix watcher")
        _release_active()
    return {"run_id": str(run_id), "target": root, "incident": incident,
            "report": report}


@router.get("/api/selffix")
def api_selffix():
    """Everything the version chip's panel shows. See the module docstring for
    why this is not folded into /api/config."""
    return selffix.snapshot()


@router.post("/api/selffix/clear")
def api_selffix_clear(x_fused: str | None = Header(default=None)):
    """Drop the modified marker without reinstalling.

    Offered because the badge is a claim about THIS machine that only the person
    at it can settle: they may have reverted the change by hand, or decided to
    keep it deliberately and not want a permanent banner about it. It clears the
    marker and nothing else — the report files stay on disk, so the record of
    what was changed survives the decision to stop being reminded about it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"cleared": selffix.clear()}
