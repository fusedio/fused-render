"""POST /api/hf/login, GET /api/hf/auth, POST /api/hf/logout — signing this
machine in to the Hugging Face Hub.

**This app stores no Hub token.** `huggingface_hub` does, and that is the whole
point of this module: the Preferences button drives hf's own browser login (RFC
8628 device code) and hf persists the result, in hf's own files, with hf's own
modes, alongside a refresh token it renews by itself. Every consumer here —
the Discover search, and every model download inside a worker — then reads it
through `get_token()`, which is what they would do for a `hf auth login` the
user ran in a terminal. There is no app-side credential: nothing to store,
nothing to mask, nothing to hand a subprocess, and nothing to leak.

**Storing a token here instead was considered and rejected** (D402): it makes the
app a credential store — a masked hint to describe the token without leaking it,
validation because the string reaches an `Authorization` header and a child's
environment, a precedence rule of our own, and an injection into every worker —
to hold a value whose format belongs to hf anyway. The reason it can be avoided
is that hf's store holds several NAMED tokens with one active, so a login made
here coexists with one the user made themselves, and hf's own writers persist it
without the network round-trip its public `login()` would add.

**The flow, and why it is shaped like this.** A device-code login has three
parts and the middle one is a human being:

1. `POST /api/hf/login` asks hf for a device code and returns the URL to open
   and the short code to confirm. It does NOT wait — the user has to go and
   authorize, which takes as long as it takes.
2. A daemon thread polls hf's token endpoint. `poll_device_token` blocks for up
   to the code's lifetime, which is why it cannot live on a request.
3. `GET /api/hf/auth` is what the page watches: the pending flow (with the code
   and the seconds it has left), or the account it ended up signed in as, or
   the reason it failed.

**One flow at a time, and a second POST joins the first** rather than starting
another. Two device codes for one user is two codes to choose between on the
Hub's page, only one of which will ever be polled — the same "join, don't
restart" rule the model supervisor applies to a load already in flight. That
takes TWO locks and the reason is in `_start_lock`: checking for a live flow and
creating one have to be atomic with each other across a Hub round-trip, and the
page polls status once a second throughout, so the pointer and the decision
cannot be guarded by the same thing.

**hf's private seams, deliberately.** `_save_oauth_token` is what persists a
device-code response *with* its refresh metadata, and `_logout_from_token`
removes ONE named entry rather than deleting every token on the machine the way
public `logout()` does. Both are underscored, and `cli/auth.py` drives the same
pair for its own machine-readable flow, so this is a seam rather than a
trespass. `test_hf_auth.py` imports them explicitly, so a release that moves
them fails a test here instead of failing a login in front of a user.

The endpoint is fixed by hf (`constants.ENDPOINT`, which honours the standard
`HF_ENDPOINT` mirror override) and the OAuth client is hf's own public device
client — this app registers nothing and holds no secret of its own.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Header

from fused_render.server.common import _error, _require_fused

router = APIRouter()

logger = logging.getLogger(__name__)

#: The variables hf reads before it reads its own files. When one of them holds
#: a token it IS the machine's token, so the page shows the button locked and
#: names the variable rather than offering a login that would change nothing.
#: Spelled here rather than imported because hf's are internal, and these two
#: names are a stable public contract of theirs.
HF_TOKEN_ENV_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


@dataclass
class _Flow:
    """One login in flight. Replaced, never mutated by the page."""

    user_code: str
    url: str
    #: Wall-clock deadline, for the countdown the page shows. The POLL's own
    #: deadline is hf's (`expires_in` inside `poll_device_token`); this one only
    #: has to agree with it closely enough to read honestly.
    expires_at: float
    started_at: float = field(default_factory=time.time)
    #: Set by the thread when it finishes, in either direction. `account` is the
    #: username hf's whoami returned, which is the only place it is available
    #: without a second network call.
    account: str | None = None
    error: str | None = None
    done: bool = False
    #: Read by the poll thread's `on_pending` hook, which raises to unwind out
    #: of hf's polling loop. Nothing else can interrupt a call parked in there.
    cancelled: bool = False


_lock = threading.Lock()
#: Serializes STARTING a login, which `_lock` cannot: the check for a flow
#: already in flight and the creation of a new one have to be atomic with each
#: other, and between them sits `request_device_code()` — a Hub round-trip.
#: Widening `_lock` over that call would block every `GET /api/hf/auth` for its
#: duration (the page polls that once a second), so the two locks divide the
#: work: `_lock` guards the pointer, this one guards the decision. Never held
#: while `_lock` is, and `_state()` never takes it, so the pair cannot deadlock.
_start_lock = threading.Lock()
_flow: _Flow | None = None
#: The account the last successful login was for, kept for the life of the
#: process. Not authority — `_state()` re-reads the token on every request — but
#: it is the only way to name the account without a whoami per poll, and the
#: page asks for state every second or two while a flow is running.
_account: str | None = None
#: The NAME hf filed that login under, kept beside it as the thing that makes it
#: checkable. hf's store is shared machine state: a `hf auth login` in a terminal
#: switches the active token to another account, and a remembered username with
#: nothing to validate it against then names the wrong person while every request
#: goes out as the new one. Comparing names rather than token values keeps the
#: credential out of this process's memory (see `_account_label`).
_account_name: str | None = None


class _Cancelled(Exception):
    """Raised out of `on_pending` to abandon a flow the user gave up on."""


def _env_token_var() -> str | None:
    """Which environment variable holds a token, or None.

    Gated on a value being present rather than the variable being set: an
    exported-but-empty `HF_TOKEN` is not a token, hf ignores it, and a page that
    locked its button against it would leave the user unable to sign in and
    unable to see why (the D148 rule, applied here).
    """
    for name in HF_TOKEN_ENV_VARS:
        if (os.environ.get(name) or "").strip():
            return name
    return None


def token() -> str | None:
    """The Hub token this machine sends, or None — hf's own resolution.

    THE reader, and the only one: `hub_models._token` calls it, and a worker
    reaches the same answer by calling `get_token()` itself inside its own
    interpreter. Never re-derived, and never cached: hf refreshes an OAuth token
    in place when it nears expiry, so a cached copy here would go stale exactly
    when it mattered.

    None when `huggingface_hub` cannot be imported at all, which is not a state
    a shipped build has (it is a core dependency) but is one a stripped
    environment can produce — and an anonymous request is a much better answer
    there than a 500.
    """
    try:
        from huggingface_hub import get_token
    except Exception:  # noqa: BLE001 - no hf means no token, not a broken page
        return None
    try:
        return get_token() or None
    except Exception:  # noqa: BLE001 - an unreadable or unrefreshable token is anonymous
        logger.debug("huggingface_hub.get_token() failed", exc_info=True)
        return None


def _active_token_name(value: str | None) -> str | None:
    """The name hf filed the ACTIVE token under, or None.

    hf's store holds several named tokens with one active, and the name is the
    only account label available offline: a device-code login is filed as the
    token's display name, or `oauth-<username>` when it has none. Matched by
    VALUE rather than trusted from a name we remembered, so the label cannot
    outlive the token it describes.
    """
    if not value:
        return None
    try:
        # `utils._auth`, not the package root: hf does not re-export this one, and
        # the name-by-value lookup below is the only offline way to label an
        # account (`test_hf_auth.py` pins the import so a move is a red test).
        from huggingface_hub.utils._auth import get_stored_tokens
    except Exception:  # noqa: BLE001
        return None
    try:
        for name, stored in (get_stored_tokens() or {}).items():
            if stored == value:
                return name
    except Exception:  # noqa: BLE001 - a malformed store is not an account
        logger.debug("huggingface_hub.get_stored_tokens() failed", exc_info=True)
    return None


def _account_label(value: str | None) -> str | None:
    """Who this machine is signed in as, as far as can be told without asking.

    The username from this process's own login when there was one, else the
    stored token's name with hf's `oauth-` prefix stripped — that prefix is
    hf's filing convention, not part of anybody's username. None means signed in
    but unnamed, which the page says as much: a whoami per status poll would put
    a network round-trip behind a settings page, and being unable to name the
    account is not worth that.

    Gated on there being a token at all, because `_account` outlives the login it
    came from: a `hf auth logout` in a terminal leaves this process still
    remembering the name, and a payload reporting `signedIn: false` beside an
    account name is describing a state that does not exist.

    **And gated on that login still being the ACTIVE one**, which is the other
    half of the same problem: hf's store is shared machine state, so a
    `hf auth login` in a terminal (or `hf auth switch`) can move the active token
    to a different account while this process still remembers its own. The
    remembered username is used only while the active token is still filed under
    the name that login produced; otherwise the label is derived from whatever IS
    active now. Matched on the NAME rather than the token value so that no
    credential has to be held in memory to make the comparison.
    """
    if not value:
        return None
    name = _active_token_name(value)
    if _account and _account_name and name == _account_name:
        return _account
    if name and name.startswith("oauth-"):
        return name[len("oauth-"):]
    return name


def _state() -> dict:
    """The GET /api/hf/auth payload.

    Reads the token EVERY time rather than trusting what a login left behind:
    hf's files are shared machine state — a `hf auth logout` in a terminal, or
    another app's login — so a cached "signed in" here is a page reporting
    something that has not been true since it was cached.
    """
    value = token()
    forced_by_var = _env_token_var()
    with _lock:
        flow = _flow
    pending = None
    # `cancelled` counts as not-pending immediately, without waiting for the
    # thread to notice: the flag is set by a request and the thread only sees it
    # on its next poll (hf sleeps the server's own interval between them), so
    # gating on `done` alone left the authorize link on screen for seconds after
    # the user pressed Cancel — offering a login that will be thrown away.
    if flow is not None and not flow.done and not flow.cancelled:
        pending = {
            "userCode": flow.user_code,
            "url": flow.url,
            "secondsLeft": max(0, int(flow.expires_at - time.time())),
        }
    return {
        "signedIn": value is not None,
        # Null while signed in means "signed in, and nothing can name the
        # account offline" — never "not signed in", which `signedIn` alone says.
        "account": _account_label(value),
        # What is actually answering: the environment (which beats hf's files,
        # in hf's own resolution as well as this app's), or hf's stored login.
        "source": "environment" if forced_by_var else ("login" if value else None),
        # The variable's NAME, never its value: that value is a credential, and
        # the name is all the page needs to say which one to unset.
        "forcedByVar": forced_by_var,
        "pending": pending,
        # The last attempt's failure, kept until the next attempt replaces it —
        # a login that failed while the user was looking at another tab must
        # still be able to say why.
        "error": flow.error if flow is not None and flow.done else None,
    }


def _poll(flow: _Flow, device_info: dict) -> None:
    """Wait for the user to authorize, then let hf persist the result.

    Runs on its own daemon thread: `poll_device_token` blocks for up to the
    device code's whole lifetime, and the thing it is waiting for is a person.
    """
    global _account, _account_name

    from huggingface_hub._login import _save_oauth_token
    from huggingface_hub.utils._oauth_device import poll_device_token

    def on_pending() -> None:
        # hf calls this after each "authorization pending" answer, outside its
        # own try, so raising here is the one way to unwind a thread parked
        # inside its polling loop.
        if flow.cancelled:
            raise _Cancelled()

    try:
        response = poll_device_token(device_info, on_pending=on_pending)
        # Checked AGAIN, here, and this is the point of it: `on_pending` only
        # runs when hf reports "authorization pending", so a poll that comes back
        # carrying an access token never consults the flag. Without this, pressing
        # Cancel and then authorizing in the still-open browser tab persists the
        # login anyway — a credential written to the machine's shared store after
        # the user asked to stop, which is the one outcome a cancel must prevent.
        # The token is simply dropped: it is hf's to reissue, and nothing here
        # has written it anywhere yet.
        if flow.cancelled:
            raise _Cancelled()
        # hf's own persistence: both of its files, its modes, and the refresh
        # token plus expiry that let `get_token()` renew this without us.
        token_name, username = _save_oauth_token(response)
    except _Cancelled:
        flow.error = None
        flow.done = True
        return
    except Exception as e:  # noqa: BLE001 - every failure is a sentence on the page
        # DeviceCodeError for denied/expired, and anything the network can do to
        # a request that runs for fifteen minutes. The class name is included
        # only when the message would otherwise be empty.
        flow.error = str(e) or e.__class__.__name__
        flow.done = True
        return
    _account = username
    _account_name = token_name
    flow.account = username
    flow.done = True


@router.get("/api/hf/auth")
def api_hf_auth():
    """Whether this machine is signed in to the Hub, and what a login is doing.

    An unguarded GET like every other read (WF-5): it reports on a credential
    without carrying one — no token, and not even the value of the environment
    variable that may be overriding it.
    """
    return _state()


@router.post("/api/hf/login")
def api_hf_login(x_fused: str | None = Header(default=None)):
    """Start hf's browser login and return the URL and code to authorize with.

    Guarded, and a POST, because it leaves the machine and starts a thread —
    the same reasoning the Hub search route documents at greater length.
    """
    global _flow

    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if (name := _env_token_var()) is not None:
        # Refused rather than run: hf reads the variable before its own files,
        # so a login here would store a token that nothing would then use, and
        # the page would be left claiming an account that is not the one making
        # requests.
        return _error(
            f"{name} is set in this app's environment, so it is already the Hub token in "
            f"use. Unset it to sign in from here.",
            status=409,
        )
    # Held across the whole check-then-create, which is what makes "one flow at
    # a time" true rather than likely: reading `live` and then swapping `_flow`
    # as two separate steps let two overlapping requests both pass the gate,
    # and the Hub then issued two device codes with two threads polling them —
    # while the page could only ever show the second, leaving the first free to
    # persist a token for a code the user never saw. A concurrent login waits
    # here for one Hub round-trip and then takes the join branch, which is the
    # answer it wanted anyway.
    with _start_lock:
        with _lock:
            live = _flow is not None and not _flow.done and not _flow.cancelled
        # Answered outside `_lock` (though still inside `_start_lock`):
        # `_state()` takes `_lock` itself and `threading.Lock` is not reentrant,
        # so building the reply in there deadlocked the second click — exactly
        # the request this branch exists to serve. `_lock` guards the POINTER;
        # every field of a `_Flow` is written by one thread and read by another
        # as a plain attribute, which is why nothing else needs it.
        if live:
            # Joined, not restarted: a second device code is a second code on
            # the Hub's page, only one of which is being polled.
            return {"joined": True, **_state()}
        try:
            from huggingface_hub.utils._oauth_device import request_device_code
        except Exception as e:  # noqa: BLE001
            return _error(f"huggingface_hub is not available here ({e.__class__.__name__})",
                          status=503)
        try:
            device_info = request_device_code()
        except Exception as e:  # noqa: BLE001 - offline, DNS, TLS, a Hub that is down
            # Nothing was reserved, so nothing has to be released: the next
            # attempt finds the same state this one did.
            return _error(f"Could not start the Hugging Face login: {e}", status=502)
        flow = _Flow(
            user_code=device_info["user_code"],
            url=device_info["verification_uri_complete"],
            expires_at=time.time() + float(device_info.get("expires_in") or 900),
        )
        thread = threading.Thread(target=_poll, args=(flow, device_info),
                                  name="hf-device-login", daemon=True)
        with _lock:
            _flow = flow
        thread.start()
        return {"joined": False, **_state()}


@router.post("/api/hf/login/cancel")
def api_hf_login_cancel(x_fused: str | None = Header(default=None)):
    """Give up on a login in flight.

    The thread notices on its next poll (hf sleeps the server's own interval
    between them), so this returns immediately and the flow disappears from the
    state a moment later. It is a daemon thread either way — an abandoned login
    can never hold the app open.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    with _lock:
        flow = _flow
    if flow is not None and not flow.done:
        flow.cancelled = True
    return _state()


@router.post("/api/hf/logout")
def api_hf_logout(x_fused: str | None = Header(default=None)):
    """Sign this machine out of the Hub — hf's store, hf's doing.

    **Removes the ACTIVE token's entry, not every token on the machine.** Public
    `logout()` deletes both of hf's files and unsets the git credential; a
    settings button that quietly signed the user out of tokens it did not create
    would be doing more than it says. `_logout_from_token` takes one name, so a
    second login the user keeps for something else survives this.
    """
    global _account, _account_name, _flow

    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if (name := _env_token_var()) is not None:
        return _error(
            f"The Hub token in use comes from {name} in this app's environment, so signing "
            f"out here cannot remove it. Unset the variable instead.",
            status=409,
        )
    value = token()
    if value is None:
        # Not an error: a Log out pressed twice, or after a `hf auth logout` in
        # a terminal, should be a no-op rather than a failure.
        return _state()
    stored_name = _active_token_name(value)
    if stored_name is None:
        return _error(
            "The Hub token in use is not one of huggingface_hub's stored logins, so it "
            "cannot be removed from here.",
            status=409,
        )
    try:
        from huggingface_hub._login import _logout_from_token

        _logout_from_token(stored_name)
    except Exception as e:  # noqa: BLE001
        return _error(f"Could not sign out: {e}", status=500)
    _account = None
    _account_name = None
    with _lock:
        # Drops the finished flow too: its `account` describes a login that no
        # longer exists, and leaving it would let `error` from an older attempt
        # reappear beside a signed-out state.
        _flow = None
    return _state()
