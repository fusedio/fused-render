"""Self-fix — the HTTP surface over `fused_render/selffix.py` (SPEC §43).

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
import os
import threading

from fastapi import APIRouter, Body, Header

from fused_render import claude_spawn, selffix
from fused_render.server import templates as _server_templates
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
# THE QUESTION IS ASKED, NOT REMEMBERED. This used to be a module-global claim
# with a token and a TTL, and it was wrong in the one way that mattered: the
# claude process is DETACHED and outlives this server, while the claim lived in
# memory and did not. A restart cleared the guard with an agent still editing —
# and the restart most likely to happen is one the fix session CAUSES, since it
# edits .py files under a dev server watching them. Persisting a lease instead
# would mean a lease file, a TTL for it, recovery at startup and a way to tell a
# stale lease from a live one: four pieces of state to keep in step with one
# fact that is already on disk.
#
# `agent._live_run(dir)` is that fact. Every run writes `RUNS/<id>/meta.json`
# with its target and a `pid` file, so "is a Claude session working in this
# directory?" is a directory scan plus a liveness check — durable across
# restarts by construction, with nothing to expire, release or recover. It also
# answers the question we actually care about rather than a proxy for it: a chat
# the user opened on the install folder BY HAND is another agent in the same
# tree, and the old claim could not see it.
#
# The lock below is not that state. It is held only for the check-and-spawn of a
# single request, so two simultaneous clicks cannot both look, both see nothing,
# and both spawn; it is released in a `finally` a second later and holds nothing
# about the session that is now running.
_spawn_lock = threading.Lock()


# Re-bound as module-level names (the apps router's convention) so a test can
# swap the spawn without reaching into another module.
_spawn_helper = claude_spawn.spawn_helper
_load_agent = claude_spawn.load_agent
_record_session_when_ready = claude_spawn.record_session_when_ready


def _claude_cli_path():
    """The CLI this machine would run, or None — re-bound for the tests, like
    the spawn above. Resolved by the same function /api/config reports from, so
    the button's wording and this refusal cannot disagree."""
    from fused_render.server import ai

    return ai.claude_cli_path()


def _live_session_in(root: str) -> str:
    """The id of a Claude run still going in `root`, or "" if there is none.

    TWO LOOKUPS, and they are not two answers to be reconciled — either one
    saying "busy" is enough, because each covers a case the other cannot.

      the run we RECORDED   `selffix.active_run()` names the fix started here,
                            however long ago. Checked by pid, so the record is
                            a pointer and never a lease.
      the runs DIRECTORY    `agent._live_run(root)` finds anything else live in
                            this tree — including a chat the user opened on the
                            install folder by hand, which nothing we record
                            could know about.

    The recorded half exists because the scan is BOUNDED: `_live_run` reads the
    newest `_LIVE_SCAN_LIMIT` runs on the machine before filtering by target,
    which is right for a chat turn ("a turn does not outlive 60 later ones") and
    wrong for this one. A fix session is the long-running case by construction,
    and a machine running scheduled tasks can start 60 runs beside it — at which
    point the scan stops seeing the very session it is meant to be guarding.

    Never raises: a lookup that cannot answer must not be the reason a fix
    cannot start. Failing OPEN is the right direction here and not a coin toss —
    everything this can fail on (the agent not loading, the runs dir being
    unreadable) fails the spawn a moment later too, with a message that says
    what actually went wrong instead of "already running".
    """
    try:
        agent = _load_agent()
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("could not load the agent to check for a live session",
                     exc_info=True)
        return ""

    recorded = selffix.active_run()
    if recorded:
        try:
            if agent._alive(os.path.join(agent.RUNS, recorded)):
                return recorded
        except Exception:  # noqa: BLE001
            logger.debug("liveness check failed for run %s", recorded, exc_info=True)

    try:
        return str(agent._live_run(root).get("run_id") or "")
    except Exception:  # noqa: BLE001
        logger.debug("live-run scan failed for %s", root, exc_info=True)
        return ""


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
        # exactly the one most likely to have changed something.
        #
        # This thread no longer releases anything: the guard is a question asked
        # of the runs directory, so a watcher that dies takes nothing with it and
        # a session it stopped following still excludes a second one for exactly
        # as long as its process is alive.
        stamp()


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

    # THE MACHINE BEFORE THE REQUEST. Whether Claude Code exists here is a fact
    # about the machine; whether the caller said what is wrong is a fact about
    # the request — and when both are unsatisfied the machine's answer is the
    # one that helps. Asking someone to describe a problem for a session that
    # cannot start on this computer is a form to fill in for nothing.
    #
    # It is also the ONLY answer the Preferences button can act on: "Set up
    # Claude Code" is offered with the description box empty (there is nothing
    # to describe yet — the CLI is the problem), so validating the body first
    # answered that click with "say what is wrong" and the user never saw the
    # install card the button exists to show.
    #
    # Same message and same status as the post-hoc path below, because it is the
    # same fact discovered a moment earlier: `spawn_helper` returns
    # CLAUDE_MISSING_ERROR when the CLI turns out to be missing, and that is
    # reported as 502.
    if _claude_cli_path() is None:
        return _error(claude_spawn.CLAUDE_MISSING_ERROR, status=502)

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
    # A READ-ONLY INSTALLATION STILL GETS A SESSION — a DIAGNOSTIC one (SF-4a).
    #
    # This used to be a 409, on the argument that a session which cannot write
    # spends minutes reading and then reports a fix that was never applied,
    # which to the user watching reads like a fix that was. That argument is
    # about a session that does not KNOW it cannot write. Told up front, the
    # same session is the most useful thing available on a machine nobody can
    # patch: it finds the cause and writes it down for someone who can, which is
    # a strictly better outcome than a refusal that leaves the user with the
    # original error and nothing else. An admin-installed copy is exactly where
    # that user is least able to help themselves.
    #
    # Everything downstream turns on this flag: the prompt (which must say so,
    # or the agent burns its turns fighting the permission error), where the
    # report goes (`records_dir` — outside a tree it could not write to), and
    # the watcher, which is skipped because nothing can change and therefore
    # nothing can be stamped.
    diagnostic = not selffix.writable()

    # Held across the LOOK and the SPAWN together, and released the moment the
    # request is done. Two clicks a millisecond apart would otherwise both find
    # the runs directory quiet and both start an agent; there is no state here
    # to leak, because what excludes the second session afterwards is the first
    # one's live process, not this lock.
    with _spawn_lock:
        busy = _live_session_in(root)
        if busy:
            return _error(
                f"a Claude session is already working on this installation "
                f"(run {busy}). Finish or stop it before starting another — two "
                "sessions editing the same files at once leave neither report "
                "true.", status=409)

        try:
            incident, report = selffix.record_incident(body)
        except OSError as exc:
            # Both records homes refused it. There is nowhere left to put the
            # file, and a session handed no incident has nothing to read.
            return _error(f"could not write the incident file: {exc}", status=500)

        # `writable()` PREDICTED; the write just PROVED. They disagree whenever
        # `os.access` says yes and something else says no — an ACL, a volume
        # remounted read-only under a running app, a full disk — and that used
        # to be a 500 on exactly the installation SF-13 exists to help. The
        # incident landing outside the tree is the proof, so believe it and run
        # the session in the mode that can actually finish.
        if not diagnostic and not selffix.in_state_dir(report):
            logger.info("self-fix: the install predicted writable and the write "
                        "disagreed — this session is diagnostic")
            diagnostic = True

        try:
            # BEFORE the session starts and never after. Two digests, not one:
            # the release's (for `reconcile`) and the tree as this session finds
            # it (what `settle` measures against) — see `selffix.begin_session`.
            # The incident write above cannot disturb either; the state dir is
            # outside the digest.
            #
            # SKIPPED WHEN DIAGNOSTIC, and not merely as an optimisation: the
            # baseline is written INTO the install tree, which is the thing this
            # branch cannot write to. There is also nothing for it to measure —
            # a digest exists to answer "did this session change the tree?", and
            # on a read-only install the answer is no by construction.
            before = "" if diagnostic else selffix.begin_session()[1]
        except OSError as exc:
            return _error(f"could not read the installation: {exc}", status=500)

        try:
            res = _spawn_helper(
                root,
                selffix.fix_prompt(incident, report, reported_error=reported_error,
                                   diagnostic=diagnostic),
                selffix.FIX_PERMISSION_MODE)
        except Exception as exc:  # noqa: BLE001 — the reason belongs in the response
            return _error(f"could not start the fix session: {exc}", status=502)
        run_id = res.get("run_id")
        if res.get("error") or not run_id:
            return _error(str(res.get("error") or "could not start the fix session"),
                          status=502)
        # Recorded INSIDE the lock and before it is dropped, so the next request
        # to take it already sees this run — the mutex only has to cover the
        # window in which nothing on disk names the session yet.
        selffix.note_session(str(run_id))

    # The marker's label for this fix. A described problem has no operation
    # name, so its first line stands in — the panel lists fixes by this, and
    # "a problem the user described" over every row says nothing.
    title = str(body.get("title") or "").strip()
    if not title:
        first_line = str(body.get("note") or "").strip().splitlines()
        title = first_line[0][:120] if first_line else ""
    # NOT WATCHED WHEN DIAGNOSTIC. The watcher exists to notice that the tree
    # changed and stamp the installation; on a read-only one it would poll a
    # digest that cannot move, and then try to write a marker into the same tree
    # it could not write the baseline to. The session is still perfectly real —
    # it just has nothing to stamp.
    if diagnostic:
        return {"run_id": str(run_id), "target": root, "incident": incident,
                "report": report, "diagnostic": True}
    try:
        threading.Thread(target=_watch_fix,
                         args=(str(run_id), incident, report, title, before),
                         daemon=True, name="fused-render-selffix-watch").start()
    except Exception:  # noqa: BLE001 — the session is already running
        # Nothing to unwind. The session is running and its run directory
        # already excludes a second one, whether or not anything is watching it
        # — which is the difference between a guard that is ASKED and one that
        # is remembered, and the reason this branch used to need an argument
        # about which harm to accept.
        #
        # One cost is left and it is not recoverable here: unwatched means
        # unstamped, so the badge will not appear for whatever this session
        # changes. The mark is a provenance claim only a watched session can
        # make (SF-7a) and is never inferred from a digest. Reaching this branch
        # at all means the interpreter could not start a thread.
        logger.exception(
            "could not start the self-fix watcher for run %s — the session is "
            "running unwatched and its changes will not be stamped", run_id)
    return {"run_id": str(run_id), "target": root, "incident": incident,
            "report": report}


@router.get("/api/selffix")
def api_selffix():
    """Everything the version chip's panel shows. See the module docstring for
    why this is not folded into /api/config."""
    snapshot = selffix.snapshot()
    # The registry's health rides along because this tab is where "something is
    # wrong with the app" lives (SF-14b), and because the toast that announces a
    # broken registry needs somewhere to send the user that holds the WHOLE
    # error and a way to copy it — a toast clamps at four lines.
    #
    # Added HERE rather than inside selffix.py, which deliberately imports
    # nothing from `server`. The router is the layer allowed to know both.
    error = _server_templates.registry_error()
    if error:
        snapshot["template_error"] = error
    return snapshot


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
