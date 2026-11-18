"""Shared fixtures for tests/test_specialists_*.py (all four streams).

Each specialists test module does `from _specialists_conftest import *` so pytest registers these in that module
only.

* `app_home` (autouse, from `_bots_conftest`): a per-test FUSED_RENDER_HOME, FUSED_RENDER_DIR (the workspace, so
  `<workspace>/specialists/` is fresh per test), an empty HOME and a missing `claude` binary.
* `client` (from `_bots_conftest`): a real uvicorn server with `create_app()`, `.get/.post` -> (status, headers,
  body), `X-Fused: 1` on POSTs. Lifespan is off, so no scheduler and no seed run unless a test calls them.
* `specialists_on` (autouse): FUSED_RENDER_SPECIALISTS=1 and the specialists caches reset. A test that wants the
  flag off does `monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")`.
  Also FUSED_RENDER_SPECIALIST_CLAUDE points at a missing file, so a side session that escapes `stub_side` fails
  fast instead of spending money.
* `needs_git`: skip a test when `git` is not on PATH (history needs it).
* `make_specialist(name="Canvas", subject=..., triggers=None)`: create one through `store.create`.
* `stub_side(monkeypatch, fn=None, answer="ok")`: replace `harness._spawn`. `fn(call)` gets the call dict
  (`argv, cwd, env, prompt, timeout_s, kind, spec_id, row_id`) and may write files in the home; it returns None
  (use `answer`), a string (the answer), or a full result dict `{"ok","answer","error","cost_usd","transcript"}`.
  Returns the list of calls.
"""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client, Client
import shutil

import pytest


def reset_specialists_caches():
    """Forget cached id -> home lookups, so a specialist from a previous test's workspace never answers here."""
    import sys

    paths = sys.modules.get("fused_render.specialists.paths")
    if paths is not None:
        paths._reset()


@pytest.fixture(autouse=True)
def specialists_on(app_home, monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    monkeypatch.setenv("FUSED_RENDER_SPECIALIST_CLAUDE", str(tmp_path / "no-such-specialist-claude"))
    # git identity for history commits in homes: the per-test HOME has no ~/.gitconfig.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    reset_specialists_caches()
    yield
    reset_specialists_caches()


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def make_specialist(name="Canvas", subject="Fused canvases", triggers=None, shipped=""):
    from fused_render.specialists import store

    return store.create(name, subject, triggers=triggers, shipped=shipped)


def stub_side(monkeypatch, fn=None, answer="ok"):
    """Replace `harness._spawn` (the one place a side session's `claude -p` process runs)."""
    from fused_render.specialists import harness

    calls = []

    def _spawn(call):
        calls.append(call)
        res = fn(call) if fn else None
        if isinstance(res, dict):
            return res
        text = res if isinstance(res, str) else answer
        return {"ok": True, "answer": text, "error": "", "cost_usd": 0.0,
                "transcript": [{"role": "prompt", "text": call.get("prompt", "")},
                               {"role": "specialist", "text": text}]}

    monkeypatch.setattr(harness, "_spawn", _spawn)
    return calls
