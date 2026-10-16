"""/api/contrib/* — the GitHub contribution loop's HTTP surface (contract
docs/CONTRIB_LOOP.md §12).

READS ARE CACHED. `GET /api/contrib/app` assembles the app page's whole
picture (AppContrib, §12.1) from the record store, the engine's local status
(git on this disk, no network), the inbox's cached list and `gh`'s cached
sign-in snapshot — it never waits on GitHub. It kicks the throttled
background refresh (`contrib/refresh.py`) and answers with what is known
now; the page polls. `GET /api/contrib/cards` and the `contrib` field on
`/api/apps` rows are cheaper still: one store read, no git at all
(`refresh.cards`/`refresh.decorate`). Reads carry no X-Fused guard, like
every other GET here: a browser already blocks a foreign page from reading
the answer.

EVERY POST PASSES ONE GATE (`_gate`), in this order:
  1. `X-Fused` (D3, `server/common.py::_require_fused`) — a blind
     cross-origin POST never reaches anything below;
  2. `path` resolves to a WORKSPACE app folder (`app_git.app_dir_for`, the
     same containment rule `/api/git-upstream`'s sync action uses) — never
     an arbitrary client-supplied directory;
  3. the route's phase flag is on (`flags.<phase>()`, prerequisites
     applied) — checked before anything is classified, so with a phase off
     the route does no work at all;
  4. the app has a record (`classify.ensure`, which creates it once) and
     its kind is one this action makes sense for — Propose and remix only
     for a GitHub-link copy, the inbox only for an own app with a repo.
The checks only the owning module can make stay in that module, and the
route returns its answer as-is: signed in to GitHub and one open proposal
(`proposals.start_*`), allowed to publish straight to the author's app
(`engine.push_to_main`), the safety review before Try (`inbox.try_open`).
Refusals are 200s shaped `{ok: false, reason, message}`, exactly like
`/api/git-upstream`'s, with plain product sentences (§0.1: no git words —
a module's PlainError sentence, gh.GitHubError's included, or one generic
sentence, the traceback to the log).

LONG OPERATIONS ARE JOBS. Drafting, sending and updating a proposal, an
inbox accept, a remix draft/publish/bring-in answer `{ok: true, job}` at
once and run on `contrib/jobs.py`'s registry, which runs one job per (kind,
app) at a time: a double-clicked Send returns the first click's job instead
of opening a second proposal (proposals' own one-open-proposal guard is the
second line). Merge jobs live in `merge_job`'s own registry;
`GET /api/contrib/job` reads either and answers one shape,
`{status: running|done|failed, message?, reason?, result?}`.

Every handler is a sync def for the reason `/api/git-upstream`'s POST is:
they shell out to git and gh, and FastAPI's threadpool keeps that off the
event loop. The contrib modules are imported lazily by name (`_mod`): the
engine and `git_upstream` import each other's halves, and a listing or a
cold server import must never pay for the git engine.
"""
from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass

from fastapi import APIRouter, Body, Header

from fused_render.contrib import refresh
from fused_render.server.common import _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# The jobs registry's own generic sentence, so a failed job and a refused
# request read the same.
_GENERIC = "Something went wrong. Try again."


def _mod(name: str):
    return importlib.import_module(f"fused_render.contrib.{name}")


def _refuse(reason: str, message: str, **extra) -> dict:
    return {"ok": False, "reason": reason, "message": message, **extra}


def _disabled() -> dict:
    return _refuse("disabled", "This isn't turned on.")


def _failure(exc: BaseException, what: str) -> dict:
    """A refusal for an exception a module raised: a PlainError's sentence
    (gh.GitHubError is one, carrying its `reason`), or the generic one —
    the traceback to the log, never to the page."""
    if isinstance(exc, _mod("jobs").PlainError) and getattr(exc, "message", None):
        return _refuse(getattr(exc, "reason", None) or "failed", exc.message)
    logger.exception("contrib %s failed", what)
    return _refuse("failed", _GENERIC)


def _call(what: str, fn, *args, **kwargs) -> dict:
    """Run a module action and normalise its answer to `{ok, …}` — a
    `start_*` helper's `{ok, job}` or refusal passes through untouched."""
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — mapped to a plain refusal
        return _failure(e, what)
    if isinstance(result, dict):
        return result if "ok" in result else {"ok": True, **result}
    return {"ok": bool(result) if isinstance(result, bool) else True}


def _job(kind: str, app_dir: str, fn, *args, after=None, **kwargs) -> dict:
    """Start `fn(*args, **kwargs)` as a contrib job and answer `{ok, job}` —
    for the one synchronous module action the route itself runs as a job
    (inbox accept). "done" means success: a module answering
    `{ok: false, …}` fails the job with that plain sentence instead of
    finishing it with a refusal buried in `result`; a raised PlainError
    fails it the same way, in `jobs._run`. Keyed on the store's spelling
    of the app (`store.key`), like every other contrib job, so dedupe holds
    whichever spelling the request arrived with. `after()` runs once `fn`
    returns, success or refusal."""
    jobs = _mod("jobs")

    def run():
        try:
            result = fn(*args, **kwargs)
        finally:
            if after is not None:
                _safely("after job", after)
        if isinstance(result, dict) and result.get("ok") is False:
            raise jobs.PlainError(result.get("message") or _GENERIC,
                                  reason=result.get("reason"))
        return result

    return {"ok": True, "job": jobs.start(kind, _mod("store").key(app_dir), run)}


# -- the gate ------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    app_dir: str
    record: dict


def _app_dir(path) -> str | None:
    """The workspace app folder `path` names (or lies inside), or None."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None
    from fused_render import app_git

    app_dir = app_git.app_dir_for(path)
    return app_dir if app_dir and os.path.isdir(app_dir) else None


def _phase_on(phase: str | tuple[str, ...]) -> bool:
    flags = _mod("flags").snapshot()
    return any(flags.get(p) for p in ((phase,) if isinstance(phase, str) else phase))


def _gate(body, x_fused, phase, kinds=None):
    """`_Target`, or the response to answer instead (see the module
    docstring for the order). `phase` is one flag name or a tuple (any one
    on); `kinds` restricts the record kind."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not isinstance(body, dict):
        body = {}
    app_dir = _app_dir(body.get("path"))
    if app_dir is None:
        return _refuse("not-an-app", "That isn't an app in your Fused folder.")
    if not _phase_on(phase):
        return _disabled()
    try:
        record = _mod("classify").ensure(app_dir)
    except Exception as e:  # noqa: BLE001
        return _failure(e, "classify")
    if not isinstance(record, dict):
        return _refuse("unknown", "Can't tell where this app came from yet. Try again in a moment.")
    if kinds is not None and record.get("kind") not in kinds:
        return _wrong_kind(kinds)
    return _Target(app_dir, record)


def _wrong_kind(kinds) -> dict:
    if kinds == ("github",):
        return _refuse("not-allowed",
                       "This only works for an app you installed from someone else's GitHub link.")
    if kinds == ("own",):
        return _refuse("not-allowed", "This only works for an app of your own.")
    return _refuse("not-allowed", "This doesn't apply to this app.")


def _number(body) -> int | None:
    n = body.get("number") if isinstance(body, dict) else None
    if isinstance(n, bool) or not isinstance(n, (int, str)):
        return None
    try:
        n = int(n)
    except ValueError:
        return None
    return n if n > 0 else None


def _text(body, key: str) -> str:
    v = body.get(key) if isinstance(body, dict) else None
    return v.strip() if isinstance(v, str) else ""


# -- reads ---------------------------------------------------------------------


def _safely(what: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — one missing piece must not blank the page
        logger.warning("contrib app %s failed", what, exc_info=True)
        return None


def _share_url(app_dir: str, copy: dict | None) -> str | None:
    # Only the url ever leaves here: github_link_state's `reason` sentences
    # are the share sheet's own and talk about pushing. `copy` is the status
    # this request already computed (a contributor's link reads its count).
    from fused_render import share_app

    state = _safely("share link", share_app.github_link_state, app_dir,
                    copy_status=copy) or {}
    return state.get("url") if state.get("ok") else None


@router.get("/api/contrib/app")
def api_contrib_app(path: str = ""):
    """AppContrib for the app containing `path` (§12.1). Cached data only.

    A folder with no record yet answers `pending: true` (the engine's own
    status says so and classifies it in the background) with nothing else
    worked out — not even the share link, whose answer depends on the
    record; the page polls. `engine.status` runs ONCE per read and is handed
    to the share link."""
    app_dir = _app_dir(path)
    if app_dir is None:
        return _refuse("not-an-app", "That isn't an app in your Fused folder.")
    flags = _mod("flags").snapshot()
    if not flags.get("sources"):
        return {**_disabled(), "flags": flags}
    refresh.note_app_opened(app_dir)
    copy = _safely("status", _mod("engine").status, app_dir)
    record = _mod("store").get(app_dir)
    pending = record is None or bool((copy or {}).get("pending"))
    out = {"ok": True, "flags": flags, "pending": pending, "copy": copy,
           "proposal": None, "inbox": None, "contributors": None,
           "github": refresh.github_identity(), "share_url": None}
    if pending:
        return out
    out["share_url"] = _share_url(app_dir, copy)
    out["proposal"] = _safely("proposal", _mod("proposals").view, app_dir)
    if flags.get("inbox") and refresh.own_with_repo(record):
        out["inbox"] = _safely("inbox", refresh.inbox_snapshot, app_dir) or {
            "items": [], "count": 0, "loading": False, "unavailable": True}
    if flags.get("loop"):
        out["contributors"] = _safely("contributors", _mod("contributors").list, app_dir)
    return out


@router.get("/api/contrib/cards")
def api_contrib_cards():
    """`{app_dir: {label, inbox, kind}}` for the Apps grid (§12.2). One
    store read, no git."""
    return refresh.cards()


_MERGE_STATUS = {"running": "running", "landing": "running", "ready": "done",
                 "accepted": "done", "discarded": "done", "failed": "failed"}


@router.get("/api/contrib/job")
def api_contrib_job(id: str = ""):
    """One shape for both registries: `{ok, status, message, reason, step,
    result}` (`step`: the job's current stage word, when it reports one).

    A merge job's `ready` is `done` with the merge job as `result` (its own
    `status` inside says ready/accepted/discarded) and its summary as
    `message`."""
    job = _mod("jobs").get(id) if id else None
    if job is not None:
        return {"ok": True, "status": job["status"], "message": job.get("message"),
                "reason": job.get("reason"), "step": job.get("step"),
                "result": job.get("result")}
    merge = _safely("merge job", _mod("merge_job").get, id) if id else None
    if isinstance(merge, dict):
        status = _MERGE_STATUS.get(merge.get("status"), "failed")
        message = merge.get("error") if status == "failed" else merge.get("summary")
        return {"ok": True, "status": status, "message": message or None,
                "reason": None, "step": None, "result": merge}
    return _refuse("unknown-job", "That task isn't running any more. Try again.",
                   status="failed", result=None)


# -- mutations: A1 ---------------------------------------------------------------


@router.post("/api/contrib/ack")
def api_contrib_ack(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """The page acknowledged a banner: `kind: "event"` (every event so far
    seen) or `"settled"` (a merged/declined proposal's) — `store.ack`."""
    t = _gate(body, x_fused, "sources")
    if not isinstance(t, _Target):
        return t
    kind = _text(body, "kind")
    if kind not in ("event", "settled"):
        return _refuse("bad-kind", "Nothing to dismiss.")
    if _mod("store").ack(t.app_dir, kind) is None:
        return _refuse("failed", _GENERIC)
    return {"ok": True}


# -- mutations: A2 your changes + Propose ------------------------------------------


@router.post("/api/contrib/propose/draft")
def api_contrib_propose_draft(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, "changes", ("github",))
    if not isinstance(t, _Target):
        return t
    return _call("propose draft", _mod("proposals").start_draft, t.app_dir)


@router.post("/api/contrib/propose/send")
def api_contrib_propose_send(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Signed-out and already-proposed are `proposals.start_send`'s refusals;
    a double click lands on the first click's job (jobs dedupe)."""
    t = _gate(body, x_fused, "changes", ("github",))
    if not isinstance(t, _Target):
        return t
    title = _text(body, "title")
    if not title:
        return _refuse("missing", "Give your proposal a title.")
    return _call("propose send", _mod("proposals").start_send, t.app_dir, title=title,
                 description=_text(body, "description"), note=_text(body, "note"))


@router.post("/api/contrib/propose/update")
def api_contrib_propose_update(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, "changes", ("github",))
    if not isinstance(t, _Target):
        return t
    return _call("propose update", _mod("proposals").start_update, t.app_dir)


@router.post("/api/contrib/propose/withdraw")
def api_contrib_propose_withdraw(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Gated on an OPEN proposal, not on kind: a remix (now "own") may still
    withdraw the proposal it sent as a GitHub-link copy. Update stays
    GitHub-link only — a remix's latest carries its credit line and new app
    id, which must never be sent into the author's app."""
    t = _gate(body, x_fused, "changes")
    if not isinstance(t, _Target):
        return t
    proposal = t.record.get("proposal")
    cache = proposal.get("cache") if isinstance(proposal, dict) else None
    if not isinstance(proposal, dict) or (cache or {}).get("state", "open") != "open":
        return _refuse("no-proposal", "There's no open proposal to withdraw.")
    return _call("withdraw", _mod("proposals").withdraw, t.app_dir)


@router.post("/api/contrib/discard")
def api_contrib_discard(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Back to the author's version (a backup is kept first — engine). With
    `withdraw`, an open proposal is closed too; the default leaves it."""
    t = _gate(body, x_fused, "changes", ("github",))
    if not isinstance(t, _Target):
        return t
    withdraw = body.get("withdraw") is True
    return _call("discard", _mod("engine").discard, t.app_dir, withdraw=withdraw)


@router.post("/api/contrib/push-main")
def api_contrib_push_main(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """The collaborator's deliberate "publish straight to the author's app";
    the engine asks GitHub whether this person may, and refuses if not."""
    t = _gate(body, x_fused, "changes", ("github",))
    if not isinstance(t, _Target):
        return t
    return _call("push-main", _mod("engine").push_to_main, t.app_dir)


# The phase that offers each kind of merge.
_MERGE_PHASE = {"needs-merge": "changes", "return": "changes",
                "own-sync": "inbox", "original": "remix"}


@router.post("/api/contrib/merge/start")
def api_contrib_merge_start(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Merge with Claude for the overlap the engine recorded
    (`engine.merge_plan` → `{purpose, onto, upstream}`)."""
    t = _gate(body, x_fused, ("changes", "inbox", "remix"), ("github", "own"))
    if not isinstance(t, _Target):
        return t
    try:
        plan = _mod("engine").merge_plan(t.app_dir)
    except Exception as e:  # noqa: BLE001
        return _failure(e, "merge plan")
    if not isinstance(plan, dict) or plan.get("purpose") not in _MERGE_PHASE:
        return _refuse("nothing-to-merge", "There's nothing to combine right now.")
    if not _phase_on(_MERGE_PHASE[plan["purpose"]]):
        return _disabled()
    try:
        job = _mod("merge_job").start(t.app_dir, purpose=plan["purpose"],
                                      onto=plan["onto"], upstream=plan["upstream"])
    except Exception as e:  # noqa: BLE001
        return _failure(e, "merge start")
    return {"ok": True, "job": job}


def _merge_job_for(t: _Target, body) -> tuple[str | None, dict | None]:
    """`(job_id, None)` when the posted merge job is one of this app's —
    whoever started it (merge/start, the engine's return-to-following,
    remix's bring-in) — else `(None, refusal)`. A job id is never trusted on
    its own: the job's own `app_dir` must be this app's store key."""
    job_id = _text(body, "job")
    merge = _safely("merge job", _mod("merge_job").get, job_id) if job_id else None
    if not isinstance(merge, dict) or merge.get("app_dir") != _mod("store").key(t.app_dir):
        return None, _refuse("unknown-job", "That merge isn't waiting any more.")
    return job_id, None


@router.post("/api/contrib/merge/accept")
def api_contrib_merge_accept(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, ("changes", "inbox", "remix"))
    if not isinstance(t, _Target):
        return t
    job_id, refused = _merge_job_for(t, body)
    if refused is not None:
        return refused
    return _call("merge accept", _mod("merge_job").accept, job_id)


@router.post("/api/contrib/merge/discard")
def api_contrib_merge_discard(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, ("changes", "inbox", "remix"))
    if not isinstance(t, _Target):
        return t
    job_id, refused = _merge_job_for(t, body)
    if refused is not None:
        return refused
    return _call("merge discard", _mod("merge_job").discard, job_id)


@router.post("/api/contrib/refresh")
def api_contrib_refresh(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Check GitHub now (the proposal's state, the inbox), at most once per
    app per 10 s. `refreshed: false` means the last check is that recent;
    the page re-reads GET /api/contrib/app either way."""
    t = _gate(body, x_fused, ("changes", "inbox"))
    if not isinstance(t, _Target):
        return t
    try:
        ran = refresh.refresh_now(t.app_dir)
    except Exception as e:  # noqa: BLE001
        return _failure(e, "refresh")
    return {"ok": True, "refreshed": ran}


# -- mutations: A3 the author's inbox ----------------------------------------------


def _inbox_gate(body, x_fused):
    """`(_Target, number)`, or `(response, None)`."""
    t = _gate(body, x_fused, "inbox", ("own",))
    if not isinstance(t, _Target):
        return t, None
    if not refresh.own_with_repo(t.record):
        return _refuse("not-allowed", "Proposals only arrive for your own apps on GitHub."), None
    number = _number(body)
    if number is None:
        return _refuse("missing", "Which proposal?"), None
    return t, number


@router.post("/api/contrib/inbox/review")
def api_contrib_inbox_review(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t, number = _inbox_gate(body, x_fused)
    if not isinstance(t, _Target):
        return t
    try:
        job = _mod("inbox").review(t.app_dir, number)
    except Exception as e:  # noqa: BLE001
        return _failure(e, "inbox review")
    return {"ok": True, "job": job}


@router.post("/api/contrib/inbox/try")
def api_contrib_inbox_try(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """A scratch copy of the proposal, next to the author's own. D18 is
    enforced by `inbox.try_open`, not here: it refuses until the review of
    the CURRENT head is ready ("review-pending"), and on a "look" verdict
    until `confirm` ("confirm")."""
    t, number = _inbox_gate(body, x_fused)
    if not isinstance(t, _Target):
        return t
    return _call("try", _mod("inbox").try_open, t.app_dir, number,
                 confirm=body.get("confirm") is True)


@router.post("/api/contrib/inbox/try/remove")
def api_contrib_inbox_try_remove(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t, number = _inbox_gate(body, x_fused)
    if not isinstance(t, _Target):
        return t
    return _call("try remove", _mod("inbox").try_remove, t.app_dir, number)


@router.post("/api/contrib/inbox/accept")
def api_contrib_inbox_accept(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """`inbox.accept` is synchronous (merges on GitHub, then syncs the
    author's copy), so the route runs it as a job — one per PROPOSAL:
    accepting #8 while #7's accept runs is a second job, not #7's. On a
    "look" verdict `inbox.accept` refuses without `confirm` (the job fails
    with reason "confirm"; the page asks, then posts again with it)."""
    t, number = _inbox_gate(body, x_fused)
    if not isinstance(t, _Target):
        return t
    return _job(f"inbox-accept-{number}", t.app_dir, _mod("inbox").accept, t.app_dir, number,
                confirm=body.get("confirm") is True,
                after=lambda: refresh.inbox_changed(t.app_dir))


@router.post("/api/contrib/inbox/decline")
def api_contrib_inbox_decline(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t, number = _inbox_gate(body, x_fused)
    if not isinstance(t, _Target):
        return t
    out = _call("decline", _mod("inbox").decline, t.app_dir, number, _text(body, "note"))
    if out.get("ok"):
        _safely("inbox changed", refresh.inbox_changed, t.app_dir)
    return out


# -- mutations: A5 remix ------------------------------------------------------------


@router.post("/api/contrib/remix/draft")
def api_contrib_remix_draft(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, "remix", ("github",))
    if not isinstance(t, _Target):
        return t
    return _call("remix draft", _mod("remix").start_draft, t.app_dir)


@router.post("/api/contrib/remix/publish")
def api_contrib_remix_publish(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    t = _gate(body, x_fused, "remix", ("github",))
    if not isinstance(t, _Target):
        return t
    name = _text(body, "name")
    if not name:
        return _refuse("missing", "Give your remix a name.")
    return _call("remix publish", _mod("remix").start_publish, t.app_dir,
                 name=name, credit=_text(body, "credit"))


@router.post("/api/contrib/remix/bring-in")
def api_contrib_remix_bring_in(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Bring the original's update into a remix — a merge job either way
    (clean: `ready` to confirm; overlap: Claude combines)."""
    t = _gate(body, x_fused, "remix", ("own",))
    if not isinstance(t, _Target):
        return t
    if not refresh.remix_of(t.record):
        return _refuse("not-allowed", "This app isn't a remix.")
    return _call("bring in", _mod("remix").start_bring_in, t.app_dir)
