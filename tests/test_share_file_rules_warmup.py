"""The `README.md`-is-disabled bug (code review finding 1): before this fix,
nothing in the product ever called `_fused_share_app.py`'s `{"action":
"rules"}` branch, so `share_file_rules`'s on-disk cache was never written and
`resolve_viewer` only ever resolved the built-in `.fused` rule. Confirmed
live: a fresh Explorer session showed the Share row disabled on a
`README.md` with "no Fused viewer opens .md files".

This drives the REAL wiring end to end rather than asserting on a pre-seeded
cache file: it takes `server/app.py`'s actual registered startup handler
(the one `create_app` wires up, found by name in `app.state.startup_handlers`
— the same seam `tests/test_app_lifespan.py` uses), runs it with the shim
call stubbed (no network, no SDK), and then hits the real
`/api/share/file/status` route for a `.md` file with NO cache file written
by the test itself. If the handler were missing, or wired to the wrong
function, or never actually persisted what the shim returned, this fails.
"""
from __future__ import annotations

import asyncio
import time

import fused_render.share_app as share_app_mod
import fused_render.share_file as share_file_mod
import fused_render.share_file_rules as share_file_rules_mod
from fused_render.server import create_app


def _isolate_state_dir(tmp_path, monkeypatch):
    """`share_file_rules.STATE_DIR` (unlike `share_app.STATE_DIR`) is read
    from `FUSED_RENDER_HOME` once at import time, not re-read per call — so
    `monkeypatch.setenv("FUSED_RENDER_HOME", ...)` alone does not stop
    `_write_cache`/`_read_cache` from hitting the REAL `~/.fused-render`
    (this is a pre-existing gap, not something this test file's env-var
    patching alone can close). Every test here that actually warms the
    cache needs this patched too, or it pollutes — and reads stale state
    left behind by — the machine's real share-rules cache file."""
    monkeypatch.setattr(share_file_rules_mod, "STATE_DIR", str(tmp_path / "home"))


def _find_handler(app, name):
    for handler in app.state.startup_handlers:
        if handler.__name__ == name:
            return handler
    raise AssertionError(f"no startup handler named {name!r}; got "
                         f"{[h.__name__ for h in app.state.startup_handlers]}")


def _sign_in(tmp_path, monkeypatch):
    creds = tmp_path / "credentials"
    creds.write_text("{}")
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(creds))


def _run_warmup(app):
    """Invoke the real registered handler, then join the daemon thread it
    starts (the seam `app.state.share_rules_warm` leaves for tests) so the
    background fetch has actually finished before we assert anything.
    `None` is a legitimate result now too — nobody signed in, so
    `_kick_warm_once` never spawned anything — and there is nothing to join
    in that case."""
    handler = _find_handler(app, "_startup_warm_share_rules")
    asyncio.run(handler())
    thread = app.state.share_rules_warm
    if thread is None:
        return
    thread.join(timeout=5)
    assert not thread.is_alive(), "warm_rules_cache did not finish in time"


def test_startup_warms_the_rule_cache_so_a_markdown_file_becomes_shareable(
        tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    _isolate_state_dir(tmp_path, monkeypatch)
    _sign_in(tmp_path, monkeypatch)

    # The stubbed shim: what a real `{"action": "rules"}` call would print,
    # if the catalog held one file-preview UDF for markdown. No cache file
    # is written by this test — only the code under test may write one.
    def fake_run_shim(request, timeout):
        assert request["action"] == "rules"
        return {"rules": [
            {"name": "Markdown_File", "token": "UDF_Markdown_File",
             "extensions": ["md"], "file_name": None, "regex": None, "order": 1},
        ]}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)

    app = create_app(start_dir=str(tmp_path))
    from fastapi.testclient import TestClient

    client = TestClient(app)

    path = tmp_path / "README.md"
    path.write_text("# hello\n")

    # Before the startup handler runs, the cache is genuinely empty on disk
    # — this reproduces the confirmed-live bug exactly. `_cached_rules` now
    # ALSO kicks a lazy background warm the first time it sees an empty
    # cache (see share_file.py) — pin that off for this one read with the
    # same guard the eager handler uses, so this test proves the EAGER
    # handler warms the cache, undisturbed by the lazy fallback racing it
    # (both would spawn instantly against this stubbed, in-process shim).
    monkeypatch.setattr(share_file_mod, "_warm_kicked", True)
    before = client.get("/api/share/file/status", params={"path": str(path)}).json()
    assert before["can_share"] is False
    assert before["viewer"] is None
    monkeypatch.setattr(share_file_mod, "_warm_kicked", False)

    _run_warmup(app)

    after = client.get("/api/share/file/status", params={"path": str(path)}).json()
    assert after["can_share"] is True
    assert after["refusal"] is None
    assert after["viewer"] == "Markdown_File"


def test_warmup_is_registered_in_create_app(tmp_path, monkeypatch):
    """A cheap wiring check: the handler exists by the name the thread-join
    seam and the review fix both depend on, so a future rename trips a
    loud, specific failure instead of the silent no-op this bug was."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path))
    _find_handler(app, "_startup_warm_share_rules")


def test_warmup_is_a_no_op_when_not_signed_in(tmp_path, monkeypatch):
    """No credentials file: warm_rules_cache must not spawn the shim at all
    (never mind an unmocked one) — signing-out users get the built-in
    `.fused` rule only, same as before this fix."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    _isolate_state_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "no-credentials"))

    called = []
    monkeypatch.setattr(share_app_mod, "_run_shim",
                        lambda *a, **k: called.append(1) or ({"rules": []}, None))

    app = create_app(start_dir=str(tmp_path))
    _run_warmup(app)
    assert called == []


def test_lean_app_with_no_prior_full_server_run_shares_a_markdown_file(
        tmp_path, monkeypatch):
    """A lean server (`fused-render open`) never runs `_startup_warm_share_
    rules` — this is exactly "a machine that has never run a full server"
    (the coordinator's own phrasing): the on-disk cache is genuinely empty,
    not just unread. `share_file._cached_rules` now kicks a background warm
    the first time it sees that, so sharing still ends up working rather
    than staying stuck on the built-in `.fused` rule forever, as it did
    before that lazy-warm fix."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    _isolate_state_dir(tmp_path, monkeypatch)
    _sign_in(tmp_path, monkeypatch)

    def fake_run_shim(request, timeout):
        assert request["action"] == "rules"
        return {"rules": [
            {"name": "Markdown_File", "token": "UDF_Markdown_File",
             "extensions": ["md"], "file_name": None, "regex": None, "order": 1},
        ]}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)

    app = create_app(start_dir=str(tmp_path), lean=True)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        path = tmp_path / "README.md"
        path.write_text("# hello\n")

        # First read: the cache is empty (fresh state dir, no prior full
        # server run) — served from the built-in rules alone for THIS call,
        # but it also kicks the lazy background warm. Whether that thread
        # finishes before this very first response comes back is a race,
        # not a contract (this stubbed, in-process shim is fast enough that
        # it sometimes does) — so poll from the start rather than asserting
        # a synchronous "before" state. What this test actually proves is
        # the eventual state: a state dir that never ran a full server still
        # ends up shareable.
        deadline = time.monotonic() + 5.0
        after = client.get("/api/share/file/status", params={"path": str(path)}).json()
        while time.monotonic() < deadline and not after["can_share"]:
            time.sleep(0.05)
            after = client.get("/api/share/file/status", params={"path": str(path)}).json()

    assert after["can_share"] is True
    assert after["refusal"] is None
    assert after["viewer"] == "Markdown_File"


def test_warmup_retries_after_a_signed_out_read_and_then_signing_in(tmp_path, monkeypatch):
    """Bugbot finding: the lazy kick in `_cached_rules()` must not be a true
    one-shot across a sign-in. Before the fix, a reader's very first
    empty-cache read — while signed out — set `_warm_kicked` even though
    `warm_rules_cache` immediately no-opped on `_logged_in()`, so no read
    for the rest of the process's life, even long after the user signed in,
    ever tried to build the real rule table again. Drives this end to end
    through the real `/api/share/file/status` route, same as the sibling
    lean/no-prior-server test above, rather than calling `_kick_warm_once`
    directly."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    _isolate_state_dir(tmp_path, monkeypatch)

    creds = tmp_path / "credentials"
    # Signed OUT to start: the credentials file does not exist yet.
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(creds))

    called = []

    def fake_run_shim(request, timeout):
        called.append(1)
        assert request["action"] == "rules"
        return {"rules": [
            {"name": "Markdown_File", "token": "UDF_Markdown_File",
             "extensions": ["md"], "file_name": None, "regex": None, "order": 1},
        ]}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)

    app = create_app(start_dir=str(tmp_path), lean=True)
    from fastapi.testclient import TestClient

    path = tmp_path / "README.md"
    path.write_text("# hello\n")

    with TestClient(app) as client:
        # First read, signed out: the lazy kick sees nobody signed in and
        # must not spend the guard — no shim call, cache stays empty (only
        # the built-in rule resolves).
        before = client.get("/api/share/file/status", params={"path": str(path)}).json()
        assert before["can_share"] is False
        assert called == [], "a signed-out read must not touch the shim at all"

        # Sign in, THEN read again — this is the read that must actually
        # kick a real warm, proving the earlier signed-out read never spent
        # the one-shot guard.
        creds.write_text("{}")

        deadline = time.monotonic() + 5.0
        after = client.get("/api/share/file/status", params={"path": str(path)}).json()
        while time.monotonic() < deadline and not after["can_share"]:
            time.sleep(0.05)
            after = client.get("/api/share/file/status", params={"path": str(path)}).json()

    assert called, "signing in never triggered a retry of the rules warm"
    assert after["can_share"] is True
    assert after["refusal"] is None
    assert after["viewer"] == "Markdown_File"
