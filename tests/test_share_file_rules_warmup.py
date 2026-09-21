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

import fused_render.share_app as share_app_mod
from fused_render.server import create_app


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
    background fetch has actually finished before we assert anything."""
    handler = _find_handler(app, "_startup_warm_share_rules")
    asyncio.run(handler())
    thread = app.state.share_rules_warm
    thread.join(timeout=5)
    assert not thread.is_alive(), "warm_rules_cache did not finish in time"


def test_startup_warms_the_rule_cache_so_a_markdown_file_becomes_shareable(
        tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
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
    # — this reproduces the confirmed-live bug exactly.
    before = client.get("/api/share/file/status", params={"path": str(path)}).json()
    assert before["can_share"] is False
    assert before["viewer"] is None

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
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "no-credentials"))

    called = []
    monkeypatch.setattr(share_app_mod, "_run_shim",
                        lambda *a, **k: called.append(1) or ({"rules": []}, None))

    app = create_app(start_dir=str(tmp_path))
    _run_warmup(app)
    assert called == []
