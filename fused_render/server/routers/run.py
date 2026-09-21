import asyncio
import logging
import uuid
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Body, Header, Request, Response

from fused_render import calls as shell_calls
from fused_render.server.common import _require_fused, resolve_py
from fused_render.executor import dumps_result, run_python
from fused_render.server import git_status
from fused_render.shell import prefetch as shell_prefetch
from fused_render.shell import prefs as shell_prefs
from fused_render.shell import mounts as shell_mounts

logger = logging.getLogger(__name__)
router = APIRouter()


def _claude_agent(resolved: str) -> bool:
    return str(resolved).replace("\\", "/").endswith("/templates/claude/agent.py")


def _queue_target(params: dict) -> str:
    """The folder this call is about, or "" when the queue has nothing to say —
    flag off, no target, or a target that resolves to no folder."""
    from fused_render import project_queue
    if not project_queue.enabled():
        return ""
    target = str(params.get("_file") or params.get("file") or "")
    if not target:
        return ""
    return project_queue.queue_key(target) or ""


def _file_owner(resolved: str, params: dict, result: dict,
                body: dict | None = None) -> None:
    """Record who owns the folder the moment a `start` or a `send` actually
    spawns — a REFILE, never a claim (Bugbot, PR #1194).

    `body` is the SAME dict `_folder_busy` saw a moment earlier in this
    request (`api_run` passes both the one it built) — the carrier for a
    claim token that gate call minted for itself; see the comment where it
    is read, below.

    THIS IS THE REAL SPAWN SITE FOR A CHAT (T3's handoff, 2026-09-17). The
    composer asks `/api/tasks/queue/admit` first, and that claims the folder
    for every send — a brand-new chat's first send under an `admit:<token>`
    placeholder, since it has neither a session nor a run id to offer yet.
    Here both names exist: `_start`/`_send` has returned the run it created
    (and, for a fresh chat, the session it minted), so this writes them down.

    **`start` and `send` are filed the same way.** Both went through the same
    admission a moment ago, so both only need their names recorded, not
    re-checked: `_file_owner` used to re-claim a `send` here, which was a
    SECOND increment of `owner.turns` for the one send admit had already
    counted once, on top of `_folder_busy`'s own claim — three claims for one
    send, and `turn_ended`'s single decrement never brought the count back to
    zero. `started` never increments; the protection against stealing a
    folder another task holds lives entirely in `_folder_busy`, which runs
    BEFORE the turn spawns.

    The task key is the session when there is one (the Tasks page keys a chat
    by its session) and the run otherwise, which is the name the page's next
    message carries. `is_free` answers to all three.

    Best-effort under one `try`, the same posture as `_folder_busy`: a filing
    that fails must never turn a run that already started into an error."""
    try:
        if not _claude_agent(resolved):
            return
        action = str(params.get("action") or "")
        if action not in ("start", "send"):
            return
        key = _queue_target(params)
        if not key:
            return
        payload = result.get("result") if isinstance(result, dict) else None
        if action == "send" and not (isinstance(payload, dict)
                                     and payload.get("sent")):
            # THE SEND DID NOT STICK — `{error}` on a dead host, `{respawn}`
            # when the live session cannot take the message as-is, or no
            # answer at all — and the page falls through to `start` WITH THE
            # SAME `queue_claim` (run-controller.ts, both send paths). The
            # gate already spent that token on this send, so the start would
            # look tokenless and `claim_took` would count the message a second
            # time: `turns` 2 for one turn, one `turn_ended`, a folder that
            # never frees (Bugbot, PR #1194, eighth round). Give the token
            # back so the start reads as the admitted send it is.
            _restore_claim(key, params, body)
        if not isinstance(payload, dict) or payload.get("error"):
            # THE START FAILED (or answered nothing): a placeholder this gate
            # minted for it must not hold the folder for `PLACEHOLDER_TTL`
            # against the user's own retry (Bugbot, PR #1194). The token
            # lives only on this request's body, so this is the one place
            # that can give the folder back.
            _drop_placeholder(key, body)
            return
        run_id = str(payload.get("run_id") or params.get("run_id") or "")
        session_id = str(payload.get("session_id") or params.get("session_id") or "")
        if not run_id and not session_id:
            # Nothing to file under: no run came of this start (Bugbot, PR
            # #1194 — same release as the error path above).
            _drop_placeholder(key, body)
            return
        # The MINTED session — `_start` answers `new_session_id or session_id`,
        # so this is the conversation the run belongs to whether it is a fresh
        # chat or a resume.
        from fused_render import queue_manager

        manager = queue_manager.get()
        # A CLAIM THIS GATE MINTED ITSELF, CONSUMED BEFORE THE REFILE
        # (2026-09-17, Bugbot PR #1194, fourth round — fix 2). A tokenless
        # nameless start on a free folder now claims a placeholder in
        # `_folder_busy` and hands its token forward on `body` — not
        # `params`, which reaches the running script verbatim and must stay
        # clean. Consuming it here, before `started`, marks that placeholder
        # `consumed`, the one proof `started` accepts for overwriting a live
        # placeholder it did not mint by name (see `started`'s docstring). A
        # missing or already-spent token (an admitted send, or one that lost
        # the placeholder in the meantime) is a no-op — `started`'s own
        # guard is the backstop either way.
        admit_token = str((body or {}).get("_queue_admit_token") or "")
        if admit_token:
            manager.consume_claim(key, admit_token)
        manager.started(key, session_id or run_id, run_id, session_id)
    except Exception:  # noqa: BLE001 — a filing that fails is not a failed run
        logger.debug("queue: could not file the owner of a start", exc_info=True)


def _restore_claim(key: str, params: dict, body: dict | None) -> None:
    """Re-file the `queue_claim` a failed `send` consumed, so the `start` the
    page falls through to — carrying the same token — is looked at, not
    counted again. Best-effort; a token nothing consumed is a no-op."""
    token = str((body or {}).get("queue_claim")
                or params.get("queue_claim") or "")
    if not token:
        return
    try:
        from fused_render import queue_manager

        queue_manager.get().restore_claim(
            key, token, run_id=str(params.get("run_id") or ""),
            session_id=str(params.get("session_id") or ""))
    except Exception:  # best effort, same posture as the gate
        logger.debug("queue: could not restore a claim", exc_info=True)


def _drop_placeholder(key: str, body: dict | None) -> None:
    """Release the `admit:` placeholder this request's gate minted, if any —
    a start that never produced a run has nothing to own the folder with."""
    token = str((body or {}).get("_queue_admit_token") or "")
    if not token:
        return
    try:
        from fused_render import queue_manager

        manager = queue_manager.get()
        owner = manager.owner(key) or {}
        task = str(owner.get("task") or "")
        if (task.startswith(queue_manager.PLACEHOLDER_PREFIX)
                and token in (owner.get("claims") or [])):
            manager.remove(task)
    except Exception:  # noqa: BLE001 — best-effort, like every filing here
        pass


def _folder_busy(resolved: str, params: dict, body: dict | None = None) -> str:
    """The refusal for a claude-agent `start`/`send` into a folder another task
    owns, or "" when the send may go. Flag off: always "". Best-effort — any
    failure to decide is a "go", which is what shipped, and it is why this whole
    body sits under one `try`: an unreadable index must cost a gate, never a
    send.

    A PER-SEND CLAIM TOKEN DECIDES WHETHER THIS DOOR LOOKS OR CLAIMS (Bugbot, PR
    #1194, second round). The first round made this door LOOK ONLY, because it
    used to claim unconditionally and that double-counted every ordinary
    admit→run send (admit's own claim, plus this one, on top of `_file_owner`'s
    refile) — `turn_ended`'s single decrement never brought a doubled count back
    to zero, and the folder never freed. But look-only reopened the OLDER bug:
    a send whose page had a stale flag read skipped `/api/tasks/queue/admit`
    entirely, so nothing had claimed this folder for it, and a second such send
    into the same free folder was never refused either — two spawns, one
    `started` overwriting the other's ownership, the first one's turns never
    counted.

    So the two sends are told apart by a TOKEN admit hands out on every
    `run: true` (`queue_manager.claim_for_send`), which the client echoes back
    here as `queue_claim`. Present and still good — `consume_claim` finds and
    removes it — this send is the one admit already counted, and the gate only
    looks (refusing if the owner has somehow changed since). Missing, or no
    longer good (already spent, or naming an owner that is gone), this send
    never went through admit at all, and the gate CLAIMS the folder itself,
    exactly as a tokenless send always had to before admission existed."""
    try:
        if not _claude_agent(resolved):
            return ""
        action = str(params.get("action") or "")
        if action not in ("start", "send"):
            return ""
        key = _queue_target(params)
        if not key:
            return ""
        from fused_render import queue_manager

        session_id = str(params.get("session_id") or "")
        run_id = str(params.get("run_id") or "")
        # ONE RECORD OF WHO OWNS THE FOLDER (PR 2, 2026-09-17), the same one the
        # composer's own door asked a moment ago (`/api/tasks/queue/admit`).
        # This used to re-derive it from the runs tree, the registry and the
        # scheduler's store — a second answer to a question the manager now
        # keeps, and one that could differ from the admission in the same
        # second.
        manager = queue_manager.get()
        logger.debug("queue gate: %s key=%s session=%r run=%r owner=%r",
                     action, key, session_id, run_id, manager.owner(key))
        # BOTH NAMES are checked, not the first one that is set: an anonymous
        # first send is filed under its run and re-filed under the session
        # once one exists, so a message carrying both must be matched against
        # both or it can be told it is behind itself. Matching is also what
        # makes the inbox-absorb case pass — a second message into the
        # conversation that is running is not somebody else.
        task_key = session_id or run_id
        claim_token = str((body or {}).get("queue_claim")
                         or params.get("queue_claim") or "")
        admitted = bool(claim_token) and manager.consume_claim(key, claim_token)
        if task_key:
            passed = (admitted
                      and any(manager.is_free(key, name)
                             for name in (task_key, run_id, session_id) if name)
                      ) or (not admitted
                            and manager.claim_took(key, task_key, run_id,
                                                   session_id)[0])
            if passed:
                return ""
            # THE FORCED CHAT'S OWN PROCESS (PR 2, 2026-09-21).
            # `POST /api/tasks/queue/force` starts a run BESIDE the folder's
            # owner on purpose — the flag-off behaviour for one message — so
            # the index goes on naming somebody else as the owner of that tree
            # for the rest of that conversation's life, and this door refused
            # its every follow-up with "another task in progress". A send whose
            # OWN run is alive is a send into the process that is already
            # there: `agent._send` absorbs it into that turn and spawns
            # nothing, so there is no second run for this gate to prevent.
            # Imported here rather than at module scope, like the manager
            # above: this router must stay importable on its own.
            from fused_render.server.routers.tasks import own_run_alive
            if own_run_alive(session_id, run_id):
                return ""
        elif admitted:
            # A brand-new chat's first send has NO name to claim or look up
            # under yet — nothing here can tell it apart from a stranger's
            # by name alone. This admission's OWN placeholder is what
            # `admitted` proves: a token this call just consumed, minted for
            # THIS send by `claim_for_send` and never carried onto a
            # stranger's owner (CORRECTED 2026-09-17, Bugbot PR #1194,
            # fourth round — see `queue_manager._claim_took`'s
            # placeholder-replace branch, which now drops rather than
            # inherits a placeholder's claims for a different name).
            return ""
        elif manager.is_free(key, ""):
            # A TOKENLESS NAMELESS START MUST STILL CLAIM (CORRECTED
            # 2026-09-17, Bugbot PR #1194, fourth round — fix 2). A page that
            # skipped `/api/tasks/queue/admit` (a stale flag read) has no
            # token to present, so this used to just LOOK (`is_free`) and let
            # the send go — leaving the folder owned by nobody while the
            # spawn was in flight. Another chat's admission then found it
            # free too, minted its own placeholder, and `started` below
            # refused to clobber that LIVE placeholder on this send's way
            # back (it cannot prove it is the admission that minted it) —
            # the process THIS send just spawned was left unowned, and the
            # other chat spawned into the same tree as well.
            #
            # So this door now claims exactly the way admit does: mint a
            # placeholder and hand its token forward on `body`
            # (`_queue_admit_token`, not `params` — that reaches the running
            # script verbatim) so `_file_owner` can consume-proof it to
            # `started` before anybody else's admission gets a look at the
            # folder. EVERY start owns the folder before the spawn, named or
            # not.
            try:
                ok, _took, token = manager.claim_for_send(
                    key, queue_manager.PLACEHOLDER_PREFIX + uuid.uuid4().hex,
                    "", "")
            except Exception:
                ok, token = False, ""
            if ok:
                if body is not None:
                    body["_queue_admit_token"] = token
                return ""
        owner = manager.owner(key) or {}
        ahead = str(owner.get("task") or owner.get("session_id") or "another task")
        return ("This folder has another task in progress (%s) — the message was "
                "not sent. Reload the page and send it again to put it in the "
                "folder's line." % ahead)
    except Exception:  # noqa: BLE001 — an undecidable gate is an open one
        return ""


@router.post("/api/run")
async def api_run(request: Request, body: dict = Body(...),
                  x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    py = body.get("py")
    html = body.get("html")
    params = body.get("params") or {}

    # Cold mount-backed reads: swap the raw-proxy source_url for the
    # store's own URL before the reader sees it. The /api/fs/raw 307
    # already sends cold ranged GETs to the store, but a redirect
    # defeats httpfs connection pooling — duckdb re-follows it per
    # range read and opens a fresh TLS connection to the store each
    # time (measured ~3x on a cold open: schema 8.5s vs 3.4s, a
    # 9-column page 14.5s vs 3.8s). Handing the reader the direct URL
    # up front lets httpfs pool its store connections normally. Done
    # here in the server, not in templates: pages keep sending the raw
    # URL and stay mount-agnostic. Warm files (prefetch landed) keep
    # the raw URL so the serve replays ranges from local disk; the
    # explicit schedule() below matters because a direct-reading run
    # never touches /api/fs/raw, which is otherwise the only place the
    # prefetch learns a file is in use.
    src = params.get("source_url")
    if isinstance(src, str):
        parts = urlsplit(src)
        fpath = dict(parse_qsl(parts.query)).get("path")
        if parts.path.endswith("/api/fs/raw") and fpath:
            upstream = shell_mounts.serve_url_for(fpath)
            if upstream is not None and not shell_prefetch.is_done(fpath):
                shell_prefetch.schedule(fpath, upstream)
                direct = await asyncio.to_thread(
                    shell_mounts.upstream_url_for, fpath)
                if direct:
                    params = dict(params, source_url=direct)

    resolved, resolve_error = resolve_py(py, html)
    if resolve_error is not None:
        return resolve_error

    # THE QUEUE'S GATE, ON THE SERVER TOO (Akshil's QA, 2026-09-16). The chat
    # asks `/api/tasks/queue/admit` before it spawns, but a page whose copy of
    # the pref was stale — a tab opened before the switch, a send made before
    # its prefs read landed — skipped the door and started a second run in a
    # busy folder. The door is the server's rule, so the server holds it here
    # as well: a `start`/`send` of the claude agent into a folder another task
    # is RUNNING in is refused with the same words the composer shows for a
    # refused admission. Only a live run refuses — a reservation is the chat's
    # own admission a moment ago, and an anonymous first send must pass it.
    refused = _folder_busy(resolved, params, body)
    if refused:
        return Response(content=dumps_result({"ok": True, "result": {"error": refused},
                                              "resolved_py": resolved}),
                        media_type="application/json")

    # Engine dispatch (D69/§20): both paths return the same wire shape
    # ({ok, result, error:{type,message,traceback}, stdout} — the fused
    # engine adds stderr/duration_ms), so pages never see which ran.
    # Resolved per request: the Preferences switch applies to the next
    # run, no restart (a set FUSED_RENDER_ENGINE pins it instead).
    engine_used = shell_prefs.effective_engine()
    if engine_used == "fused":
        from fused_render import engine as _engine

        work = _engine.run_python(resolved, params)
    else:
        # The built-in executor blocks on a subprocess; keep the event
        # loop free (the endpoint is async now for the engine's sake).
        work = asyncio.to_thread(run_python, resolved, params)
    result = await work
    # THE FOLDER'S OWNER IS FILED HERE, at the one place a chat's turn is
    # actually spawned — see `_file_owner`. Nothing before this call has a run
    # id or a session to file under for a brand-new chat's first send.
    _file_owner(resolved, params, result, body)
    # A script that ran may have written anything, anywhere — including
    # through the git template's stage/unstage/commit (templates/git/ops.py),
    # which runs on this same subprocess-per-call path and so cannot reach
    # into THIS process's `git_status` cache itself (see that cache's own
    # `invalidate_status_cache` docstring). Dropping it here, unconditionally,
    # is the in-process mirror of `noteFsChanged()` (static/runtime.js) firing
    # on every runPython completion — success or failure, since a script that
    # wrote three files and then raised still changed the disk, and the same
    # is true of a git op that partially applied before erroring. Without
    # this, a listing fetched moments before the run — and still within
    # `_STATUS_CACHE_TTL_S` — would replay its now-stale answer to the very
    # refresh Job B's dir-watch fix triggers right after this call returns.
    git_status.invalidate_status_cache()
    # Hand the run's detail to the in-flight call record (calls.py): the
    # resolved .py, the params, the engine, and — on failure — the
    # traceback and output tails a user has since clicked away from. The
    # handler enriches; the middleware writes. (Whether the client hung up
    # mid-run is decided by the middleware — a route CANNOT see it; the
    # NOT IMPLEMENTED note above `no_cache_and_log` says why.)
    shell_calls.enrich_run(
        getattr(request.state, "fused_call", None),
        resolved=resolved, params=params, engine=engine_used, result=result,
    )
    # Tell the runtime which absolute file actually ran so it can watch it
    # for auto-reload (LR-2). Set on failed runs too, so a broken py that
    # gets fixed still triggers a reload.
    result["resolved_py"] = resolved
    # dumps_result, not JSONResponse: the in-process executor path already
    # serialized the payload (it has to, to validate it), so this reuses
    # that string instead of encoding a multi-MB result a second time. The
    # bytes are identical to JSONResponse's for every other result.
    return Response(content=dumps_result(result), media_type="application/json")
