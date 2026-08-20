"""Signing this machine in to the Hugging Face Hub (/api/hf/*, D385).

**The Hub is never called.** `request_device_code` and `poll_device_token` are
replaced per test, because what is under test is what this app DOES with a
device flow — that it joins a second click to the first, that it reports a
pending code honestly, that a cancel unwinds a thread parked inside hf's poll —
and none of that is a fact about huggingface.co. `HF_HOME` is redirected so no
test reads or writes a developer's real login, and the token environment is
emptied so nobody's exported `HF_TOKEN` decides what these assert.

The section at the bottom is the one that would be easiest to lose: this
feature's whole justification is that **the app stores no token**, so there are
tests that no credential appears in any response, that prefs.json never gains
one, and that hf's private seams this router drives still exist — a release that
moves `_save_oauth_token` or `_logout_from_token` must fail here rather than in
front of a user halfway through a login.
"""
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import hf_auth

FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


@pytest.fixture(autouse=True)
def _isolated_hf(monkeypatch, tmp_path):
    """A private hf home and no ambient token, per test.

    `HF_HOME` has to be set before hf resolves its constants, and hf caches them
    at import — so the paths are patched directly rather than trusting the
    variable, which is also what makes this work when hf was imported by an
    earlier test.
    """
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    home = tmp_path / "hf-home"
    home.mkdir()
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(home / "token"))
    monkeypatch.setattr(constants, "HF_STORED_TOKENS_PATH", str(home / "stored_tokens"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "render-home"))
    # Module state is process-global (one login at a time), so a test that left a
    # flow behind must not decide the next one's answer.
    monkeypatch.setattr(hf_auth, "_flow", None)
    monkeypatch.setattr(hf_auth, "_account", None)
    yield


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _device(**over):
    info = {"device_code": "dev-code", "user_code": "WXYZ-ABCD",
            "verification_uri": "https://huggingface.co/login/device",
            "verification_uri_complete": "https://huggingface.co/login/device?code=WXYZ-ABCD",
            "interval": 0, "expires_in": 900}
    info.update(over)
    return info


def _stub_flow(monkeypatch, *, poll):
    """Replace the two hf protocol calls this router drives.

    Patched on `huggingface_hub.utils._oauth_device`, the module the router
    imports them FROM at call time — patching a name the router had already
    bound would test the patch rather than the router.
    """
    from huggingface_hub.utils import _oauth_device

    monkeypatch.setattr(_oauth_device, "request_device_code", lambda: _device())
    monkeypatch.setattr(_oauth_device, "poll_device_token", poll)


def _wait(predicate, timeout=5.0):
    """Poll until the background login thread has done its work."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- the flow ---------------------------------------------------------------------


def _store(name, value):
    from huggingface_hub._login import _save_token, _set_active_token

    _save_token(token=value, token_name=name)
    _set_active_token(token_name=name, add_to_git_credential=False)


def test_a_machine_with_no_token_says_so(client):
    body = client.get("/api/hf/auth").json()
    assert body == {"signedIn": False, "account": None, "source": None,
                    "forcedByVar": None, "pending": None, "error": None}


def test_login_returns_the_code_to_authorize_with_and_then_signs_in(client, monkeypatch):
    """The whole happy path. hf persists the token — this app writes nothing —
    and the account name comes from the login itself, which is the only place it
    is available without a second network call."""
    # The poll BLOCKS until the test lets it through, standing in for the human
    # who has to go and authorize. An instant stub would let the thread finish
    # before the POST built its reply, and the pending code — the entire point
    # of that reply — would be gone by the time it was asserted on. An Event
    # rather than a sleep: the sequence is the thing under test, not a duration.
    authorized = threading.Event()

    def poll(info, on_pending=None):
        assert authorized.wait(5), "the test never released the login"
        return {"access_token": "hf_oauth_token", "refresh_token": "hf_refresh",
                "expires_in": 3600}

    _stub_flow(monkeypatch, poll=poll)
    # hf validates the token against whoami before storing it; that is the Hub,
    # so it is stubbed at the seam hf's own persistence calls.
    import huggingface_hub.hf_api as hf_api

    monkeypatch.setattr(hf_api, "whoami", lambda token=None, **k: {
        "name": "isaac", "auth": {"accessToken": {"displayName": "fused-render"}}})

    started = client.post("/api/hf/login", headers=FUSED).json()
    assert started["joined"] is False
    assert started["pending"]["userCode"] == "WXYZ-ABCD"
    assert started["pending"]["url"].endswith("code=WXYZ-ABCD")
    assert 0 < started["pending"]["secondsLeft"] <= 900
    assert started["signedIn"] is False

    authorized.set()  # ...the user authorizes in their browser
    assert _wait(lambda: client.get("/api/hf/auth").json()["pending"] is None)
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is True
    assert body["account"] == "isaac"
    assert body["source"] == "login"
    assert body["error"] is None
    # hf's store holds it, and hf's reader finds it — which is how a worker will.
    from huggingface_hub import get_token

    assert get_token() == "hf_oauth_token"


def test_a_second_click_joins_the_login_already_running(client, monkeypatch):
    """Two device codes for one user is two codes on the Hub's page with only one
    of them being polled — the same join-don't-restart rule the model supervisor
    applies to a load in flight."""
    codes = []

    def poll(info, on_pending=None):
        while True:  # parked until the test cancels, like a real unauthorized flow
            if on_pending is not None:
                on_pending()
            time.sleep(0.01)

    from huggingface_hub.utils import _oauth_device

    def request():
        codes.append(len(codes))
        return _device(user_code=f"CODE-{len(codes)}")

    monkeypatch.setattr(_oauth_device, "request_device_code", request)
    monkeypatch.setattr(_oauth_device, "poll_device_token", poll)

    first = client.post("/api/hf/login", headers=FUSED).json()
    second = client.post("/api/hf/login", headers=FUSED).json()
    assert first["joined"] is False and second["joined"] is True
    assert second["pending"]["userCode"] == first["pending"]["userCode"] == "CODE-1"
    assert len(codes) == 1  # ...and the Hub was asked exactly once
    client.post("/api/hf/login/cancel", headers=FUSED)


def test_two_overlapping_logins_ask_the_hub_for_ONE_code(client, monkeypatch):
    """The join gate has to be atomic with the create, not a read taken before it.

    Reported by Bugbot on #676: `live` was read under the lock, the lock was
    dropped for the Hub round-trip, and `_flow` was swapped afterwards — so two
    overlapping requests both passed the gate. The Hub then issued two device
    codes with two threads polling them while the page could only ever show the
    second, which left the first free to persist a token for a code the user
    never saw. Reachable with one user: a browser tab and the desktop window are
    two surfaces onto the same server.

    Driven with a SLOW `request_device_code`, because a fast one hides the bug —
    the interleaving only exists while that call is in flight.
    """
    calls = []

    def slow_request():
        calls.append(1)
        time.sleep(0.3)  # the Hub round-trip the old gate left unguarded
        return _device(user_code=f"CODE-{len(calls)}")

    from huggingface_hub.utils import _oauth_device

    monkeypatch.setattr(_oauth_device, "request_device_code", slow_request)
    monkeypatch.setattr(_oauth_device, "poll_device_token",
                        lambda info, on_pending=None: _park(on_pending))

    replies = []
    threads = [threading.Thread(target=lambda: replies.append(
        client.post("/api/hf/login", headers=FUSED).json())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(calls) == 1, "the Hub was asked for a second device code"
    assert sorted(r["joined"] for r in replies) == [False, True]
    # Both surfaces are looking at the SAME code — the one being polled.
    assert {r["pending"]["userCode"] for r in replies} == {"CODE-1"}
    client.post("/api/hf/login/cancel", headers=FUSED)


def _park(on_pending):
    """Stand in for a login nobody has authorized yet: hf's poll, blocking until
    the flag its `on_pending` hook watches is set."""
    while True:
        if on_pending is not None:
            on_pending()
        time.sleep(0.01)


def test_cancel_unwinds_a_thread_parked_inside_hfs_poll(client, monkeypatch):
    """`poll_device_token` blocks for the code's whole lifetime and takes no
    cancel argument; raising from its `on_pending` hook is the only way out, and
    hf calls that hook outside its own try. If that ever stops being true, a
    cancelled login keeps polling until the code expires."""
    def poll(info, on_pending=None):
        while True:
            if on_pending is not None:
                on_pending()   # raises _Cancelled once the flag is set
            time.sleep(0.01)

    _stub_flow(monkeypatch, poll=poll)
    client.post("/api/hf/login", headers=FUSED)
    assert client.get("/api/hf/auth").json()["pending"] is not None
    client.post("/api/hf/login/cancel", headers=FUSED)
    assert _wait(lambda: client.get("/api/hf/auth").json()["pending"] is None)
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is False
    # A cancel is not a failure: nothing to explain, so no error banner.
    assert body["error"] is None


def test_a_cancelled_login_is_not_persisted_even_if_the_hub_authorizes_it(
        client, monkeypatch):
    """Bugbot on #676. `flow.cancelled` was only consulted inside `on_pending`,
    which hf calls ONLY when it gets an "authorization pending" answer — so a
    poll that came back carrying an access token never saw the flag, and pressing
    Cancel and then authorizing in the still-open browser tab persisted the login
    anyway. That writes a credential into the machine's shared hf store after the
    user asked to stop, which is the one thing a cancel has to prevent.

    Driven with events rather than sleeps: the stub parks inside the poll until
    the test has issued the cancel, then returns a token — the exact interleaving
    the bug needs.
    """
    entered, release = threading.Event(), threading.Event()

    def poll(info, on_pending=None):
        entered.set()
        assert release.wait(5), "the test never released the poll"
        return {"access_token": "hf_must_never_be_saved", "expires_in": 3600}

    _stub_flow(monkeypatch, poll=poll)
    # whoami SUCCEEDS on purpose. Failing it here would block persistence by
    # accident and the test would pass with the guard removed — which is exactly
    # what a first draft of this test did. The whole path has to be able to
    # persist, so that `get_token()` staying empty is evidence of the guard and
    # not of a stubbed-out failure.
    import huggingface_hub.hf_api as hf_api

    monkeypatch.setattr(hf_api, "whoami", lambda token=None, **k: {
        "name": "isaac", "auth": {"accessToken": {"displayName": "fused-render"}}})

    client.post("/api/hf/login", headers=FUSED)
    assert entered.wait(5)
    client.post("/api/hf/login/cancel", headers=FUSED)
    release.set()

    assert _wait(lambda: client.get("/api/hf/auth").json()["pending"] is None)
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is False
    assert body["account"] is None
    from huggingface_hub import get_token

    assert get_token() is None, "a cancelled login left a token on the machine"


def test_cancel_stops_offering_the_link_immediately(client, monkeypatch):
    """The page must stop showing the authorize link the moment Cancel is pressed.

    The flag is set by the request; the poll thread only notices on its next
    round (hf sleeps the server's interval between them), so gating `pending` on
    `done` alone left the link up for seconds, offering a login already thrown
    away. Asserted on the cancel's OWN response, which is built before any thread
    could have reacted.
    """
    _stub_flow(monkeypatch, poll=lambda info, on_pending=None: _park(on_pending))
    assert client.post("/api/hf/login", headers=FUSED).json()["pending"] is not None
    assert client.post("/api/hf/login/cancel", headers=FUSED).json()["pending"] is None


def test_the_account_name_follows_a_login_made_outside_this_app(client, monkeypatch):
    """Bugbot on #676. hf's store is shared machine state: a `hf auth login` in a
    terminal moves the active token to another account. The remembered username
    was returned whenever ANY token existed, so Preferences kept naming the old
    user while every request went out as the new one.

    The remembered name is now used only while the login it came from is still the
    active one — compared by hf's own token NAME, so no credential is held in this
    process to make the comparison.
    """
    _stub_flow(monkeypatch, poll=lambda info, on_pending=None: {
        "access_token": "hf_mine", "expires_in": 3600})
    import huggingface_hub.hf_api as hf_api

    monkeypatch.setattr(hf_api, "whoami", lambda token=None, **k: {
        "name": "isaac", "auth": {"accessToken": {"displayName": "fused-render"}}})
    client.post("/api/hf/login", headers=FUSED)
    assert _wait(lambda: client.get("/api/hf/auth").json()["signedIn"])
    assert client.get("/api/hf/auth").json()["account"] == "isaac"

    # ...now somebody logs in as a different account from a terminal.
    _store("oauth-acme", "hf_theirs")
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is True
    assert body["account"] == "acme", "the label still named the previous login"


def test_a_denied_or_expired_login_says_why(client, monkeypatch):
    from huggingface_hub.errors import DeviceCodeError

    def poll(info, on_pending=None):
        raise DeviceCodeError("Authorization was denied. Please try again.")

    _stub_flow(monkeypatch, poll=poll)
    client.post("/api/hf/login", headers=FUSED)
    assert _wait(lambda: client.get("/api/hf/auth").json()["error"] is not None)
    body = client.get("/api/hf/auth").json()
    assert "denied" in body["error"]
    assert body["signedIn"] is False and body["pending"] is None


def test_an_unreachable_hub_is_a_502_not_a_500(client, monkeypatch):
    from huggingface_hub.utils import _oauth_device

    def boom():
        raise OSError("no route to host")

    monkeypatch.setattr(_oauth_device, "request_device_code", boom)
    reply = client.post("/api/hf/login", headers=FUSED)
    assert reply.status_code == 502
    assert "Could not start" in reply.json()["error"]


def test_the_writes_are_guarded(client):
    # D3: reads are unguarded, anything with an outward effect carries X-Fused.
    for route in ("/api/hf/login", "/api/hf/login/cancel", "/api/hf/logout"):
        assert client.post(route).status_code == 403, route
    assert client.get("/api/hf/auth").status_code == 200


# -- logging out removes OUR entry, not every token on the machine ----------------


def test_logout_leaves_a_second_login_alone(client):
    """hf's public `logout()` deletes both of its files and unsets the git
    credential. A settings button that quietly signed the user out of tokens it
    did not create would be doing more than it says, so this removes the ACTIVE
    entry by name."""
    from huggingface_hub import get_token
    from huggingface_hub.utils._auth import get_stored_tokens

    _store("my-laptop", "hf_their_own")
    _store("fused-render", "hf_ours")
    assert get_token() == "hf_ours"

    body = client.post("/api/hf/logout", headers=FUSED).json()
    assert body["signedIn"] is False
    assert get_token() is None
    assert list(get_stored_tokens()) == ["my-laptop"]  # theirs survives


def test_a_signed_out_state_never_carries_a_stale_account_name(client, monkeypatch):
    """`_account` outlives the login it came from — a `hf auth logout` in a
    terminal, or another tool's, leaves this process still remembering the name.
    A payload reporting `signedIn: false` beside an account is describing a state
    that does not exist."""
    monkeypatch.setattr(hf_auth, "_account", "isaac")
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is False
    assert body["account"] is None


def test_an_oauth_login_from_an_earlier_run_is_named_without_hfs_prefix(client, monkeypatch):
    """The account label after a RESTART, which is the only time it is derived
    rather than remembered.

    A device-code login with no display name is filed by hf as
    `oauth-<username>`; that prefix is hf's filing convention, not part of
    anybody's name. With `_account` empty — a fresh process reading a login made
    before it started — the label comes from the stored name, and it has to come
    back as the username.
    """
    monkeypatch.setattr(hf_auth, "_account", None)
    _store("oauth-isaac", "hf_from_a_previous_run")
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is True
    assert body["account"] == "isaac"


def test_logging_out_cannot_remove_a_token_hf_never_filed(client, monkeypatch, tmp_path):
    """A token file with no matching entry in hf's stored-tokens index.

    Not hypothetical: `huggingface_hub` 0.25 wrote `token` and nothing else, so
    every machine that logged in before hf's named-token store existed is in
    exactly this state until it logs in again. `_logout_from_token` takes a name,
    and there is no name here — so this says so instead of raising, or worse,
    deleting a file it cannot account for.
    """
    from huggingface_hub import constants

    with open(constants.HF_TOKEN_PATH, "w", encoding="utf-8") as handle:
        handle.write("hf_written_by_an_older_hf\n")
    assert client.get("/api/hf/auth").json()["signedIn"] is True
    reply = client.post("/api/hf/logout", headers=FUSED)
    assert reply.status_code == 409
    assert "stored logins" in reply.json()["error"]
    # ...and the file is left exactly where it was, not half-removed.
    with open(constants.HF_TOKEN_PATH, encoding="utf-8") as handle:
        assert handle.read().strip() == "hf_written_by_an_older_hf"


def test_logging_out_twice_is_not_an_error(client):
    _store("fused-render", "hf_ours")
    assert client.post("/api/hf/logout", headers=FUSED).json()["signedIn"] is False
    reply = client.post("/api/hf/logout", headers=FUSED)
    assert reply.status_code == 200
    assert reply.json()["signedIn"] is False


# -- an environment token wins, and the page is told rather than shown a dead button


def test_an_environment_token_is_named_and_blocks_a_pointless_login(client, monkeypatch):
    """hf reads `HF_TOKEN` before its own store, so a login while one is set
    would save a token nothing would use and leave the page naming an account
    that is not the one making requests."""
    monkeypatch.setenv("HF_TOKEN", "hf_from_the_environment")
    body = client.get("/api/hf/auth").json()
    assert body["signedIn"] is True
    assert body["source"] == "environment"
    # The variable's NAME. Its value is a credential and never leaves the server.
    assert body["forcedByVar"] == "HF_TOKEN"
    assert "hf_from_the_environment" not in json.dumps(body)

    for route in ("/api/hf/login", "/api/hf/logout"):
        reply = client.post(route, headers=FUSED)
        assert reply.status_code == 409, route
        assert "HF_TOKEN" in reply.json()["error"]


def test_the_older_variable_name_counts_too(client, monkeypatch):
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "hf_older_name")
    body = client.get("/api/hf/auth").json()
    assert body["forcedByVar"] == "HUGGING_FACE_HUB_TOKEN"


def test_an_empty_variable_does_not_block_anything(client, monkeypatch):
    """D148's rule at a new site: forced means IN FORCE, not merely set. hf
    ignores an exported-but-empty value, and a page that greyed its button out
    for one would leave the user unable to sign in and unable to see why."""
    monkeypatch.setenv("HF_TOKEN", "   ")
    body = client.get("/api/hf/auth").json()
    assert body["forcedByVar"] is None
    assert body["signedIn"] is False


# -- the app stores no credential, which is this feature's whole point -----------


def test_no_token_is_stored_by_this_app_or_returned_to_the_page(client, monkeypatch, tmp_path):
    _stub_flow(monkeypatch, poll=lambda info, on_pending=None: {
        "access_token": "hf_super_secret", "expires_in": 3600})
    import huggingface_hub.hf_api as hf_api

    monkeypatch.setattr(hf_api, "whoami", lambda token=None, **k: {"name": "isaac"})

    client.post("/api/hf/login", headers=FUSED)
    assert _wait(lambda: client.get("/api/hf/auth").json()["signedIn"])

    # Not in any response...
    for route in ("/api/hf/auth", "/api/prefs"):
        assert "hf_super_secret" not in client.get(route).text
    # ...and not in this app's own state, which is the claim that matters: the
    # token pref this replaced (D385) lived exactly there.
    render_home = tmp_path / "render-home"
    for path in render_home.rglob("*") if render_home.exists() else []:
        if path.is_file():
            assert "hf_super_secret" not in path.read_text(errors="replace"), path
    prefs = render_home / "prefs.json"
    if prefs.exists():
        assert "hf_token" not in json.loads(prefs.read_text())


def test_prefs_carries_no_hub_token_surface(client):
    """The pref is gone, not merely unused. A payload that still advertised one
    would invite a page to write it, and a written one would be a credential
    this app is once again responsible for."""
    body = client.get("/api/prefs").json()
    assert "hf" not in body
    reply = client.put("/api/prefs", json={"hf_token": "hf_x"}, headers=FUSED)
    assert reply.status_code == 400
    assert "no known preference" in reply.json()["error"]


def test_the_private_hf_seams_this_router_drives_still_exist():
    """The router calls four underscored names. They are a seam — hf's own
    `cli/auth.py` drives the same pair — but a seam is not a contract, so this
    test is what turns a library upgrade that moves them into a red test here
    instead of a login that fails in front of a user."""
    from huggingface_hub._login import (
        _logout_from_token,
        _save_oauth_token,
        _save_token,
        _set_active_token,
    )
    from huggingface_hub.utils._auth import get_stored_tokens
    from huggingface_hub.utils._oauth_device import poll_device_token, request_device_code

    for fn in (_logout_from_token, _save_oauth_token, _save_token, _set_active_token,
               get_stored_tokens, poll_device_token, request_device_code):
        assert callable(fn)


def test_raising_from_on_pending_is_what_cancels_a_login(monkeypatch):
    """The one hf behaviour the router leans on that no signature shows.

    `poll_device_token` blocks for the device code's whole lifetime and takes no
    cancel argument, so the ONLY way out of a thread parked inside it is to raise
    from the `on_pending` hook — which works because hf calls that hook outside
    its own `except`. Tested against hf's real polling loop with the Hub's answer
    stubbed one layer lower, rather than by reading hf's source: the property is
    "the exception escapes", and that is what a caller depends on.
    """
    from huggingface_hub.utils import _oauth_device

    class _Pending:
        status_code = 200

        @staticmethod
        def json():
            return {"error": "authorization_pending"}

    class _Session:
        @staticmethod
        def post(*a, **k):
            return _Pending()

    monkeypatch.setattr(_oauth_device, "get_session", lambda: _Session())

    class Stop(Exception):
        pass

    def on_pending():
        raise Stop()

    info = {"device_code": "d", "user_code": "U", "verification_uri": "u",
            "verification_uri_complete": "u", "interval": 0, "expires_in": 5}
    with pytest.raises(Stop):
        _oauth_device.poll_device_token(info, on_pending=on_pending)
