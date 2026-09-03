"""Tests for fused_render/github_setup.py and GET/POST /api/github/status.

Ported from tests/test_claude_health.py's shape: the point of this module is
to say something TRUE about the machine before anything needs the `gh` CLI,
so the tests are mostly about the parse of `gh auth status` — signed in,
signed out, a missing binary, a broken one — rather than about spawning a
real `gh`, which none of these do.

`gh auth status` differs from `claude auth status` in a way that matters to
every test here: it prints human-readable text to STDERR, not JSON, and its
own EXIT CODE is authoritative (0 signed in, non-zero signed out) — there is
no CLI-too-old ambiguity to preserve, unlike claude_health.signed_in's
None-for-unknown tri-state. Exit code and output presence are the only two
facts the parser reads.
"""
import json
import os
import sys

import pytest

from fused_render import github_setup


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own shell home (so the cache file is its own) and a
    PATH that inherits nothing from the machine running the suite — which may
    well have a real `gh` installed and signed in."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(github_setup.BIN_ENV, raising=False)


def _fake_cli(tmp_path, name="gh", executable=True):
    """An executable stand-in for the CLI, in its own dir. Returns the path.

    Never actually spawned by anything below (every test here patches
    `subprocess.run` rather than running it) — it only has to be a file
    `resolve()`'s real `shutil.which` can find on PATH."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    if os.name == "nt" and not name.lower().endswith((".exe", ".cmd", ".bat")):
        name += ".exe"
    p = d / name
    p.write_text("#!/bin/sh\necho gh version 2.63.0 (2024-10-30)\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


# -- resolution -----------------------------------------------------------


def test_override_wins(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setenv(github_setup.BIN_ENV, "/opt/custom/gh")
    assert github_setup.resolve() == ("/opt/custom/gh", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    resolved, source = github_setup.resolve()
    assert os.path.normcase(resolved) == os.path.normcase(bin_path)
    assert source == "path"


def test_nothing_installed_resolves_to_nothing(monkeypatch):
    # The candidate list includes real system locations (e.g. /usr/bin/gh)
    # for the case where the app's PATH is stripped but the machine has a real
    # install — which the suite runner's own machine may well have. Emptied
    # here so this test asserts "no candidate resolves", not "this developer's
    # laptop has no gh", the same isolation claude_health's tests get for free
    # from POSIX_CANDIDATES holding no globally-installed system paths.
    monkeypatch.setattr(github_setup, "candidates", lambda: ())
    assert github_setup.resolve() == (None, None)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fakes os.name='posix' on a real filesystem — see the equivalent "
           "skip in test_claude_health.py for why that is unsafe on real "
           "Windows.",
)
def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    """A Finder/Dock-launched .app inherits the supervisor's PATH, not a
    shell's, so the known install dirs — including this app's own
    ~/.fused-render/bin, Task 2's install target — are all that is left."""
    home = tmp_path / "userhome"
    (home / ".fused-render" / "bin").mkdir(parents=True)
    cli = home / ".fused-render" / "bin" / "gh"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(github_setup.os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1))
    monkeypatch.setattr(github_setup.os, "name", "posix")
    assert github_setup.resolve() == (str(cli), "candidate")


def test_fused_render_bin_dir_is_a_posix_candidate():
    """Task 2 (not this one) installs `gh` into ~/.fused-render/bin; nothing
    populates it yet, but the candidate list must already know to look there
    so a Dock-launched app finds it the day it exists."""
    assert any(c.endswith(".fused-render/bin/gh")
               for c in github_setup.POSIX_CANDIDATES)


# -- version parsing --------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("gh version 2.63.0 (2024-10-30)", "2.63.0"),
    ("gh version 2.63.0 (2024-10-30)\n", "2.63.0"),
    ("2.4.0", "2.4.0"),
    ("", None),
    ("no digits here", None),
])
def test_parse_version(text, want):
    assert github_setup.parse_version(text) == want


def test_probe_version_reads_stdout(monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode, stdout, stderr = 0, "gh version 2.63.0 (2024-10-30)\n", ""
        return R()

    monkeypatch.setattr(github_setup.subprocess, "run", fake_run)
    assert github_setup.probe_version("/x/gh") == "2.63.0"


def test_probe_version_survives_a_missing_or_hung_binary(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(github_setup.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("gh", 1)))
    assert github_setup.probe_version("/x/gh") is None
    monkeypatch.setattr(github_setup.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert github_setup.probe_version("/x/gh") is None


# -- `gh auth status` parsing ------------------------------------------------
#
# Real output shapes, captured from `gh auth status`. It prints to STDERR
# (not JSON, unlike `claude auth status`), and the exit code is authoritative:
# 0 means signed in, non-zero means signed out. Unlike claude_health.signed_in
# there is no None-for-unknown tri-state to preserve here — gh's exit code
# always tells us, so an unparseable-but-zero-exit answer is the only truly
# ambiguous case, and even that degrades to False rather than None (see the
# module docstring's note that gh's exit code IS authoritative).

_SIGNED_IN_OUTPUT = """github.com
  ✓ Logged in to github.com account octocat (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo'
"""

_SIGNED_OUT_OUTPUT = (
    "You are not logged into any GitHub hosts. Run `gh auth login` to "
    "authenticate.\n"
)

_MULTI_HOST_OUTPUT = """github.com
  ✓ Logged in to github.com account octocat (keyring)
  - Active account: true

my.ghe.example.com
  ✓ Logged in to my.ghe.example.com account someone-else (keyring)
  - Active account: true
"""


def _auth_says(monkeypatch, stderr, returncode=0, stdout=""):
    def fake_run(argv, **kwargs):
        assert argv[1:] == ["auth", "status"], argv
        return type("R", (), {"returncode": returncode, "stdout": stdout,
                              "stderr": stderr})()

    monkeypatch.setattr(github_setup.subprocess, "run", fake_run)


def test_parse_auth_status_signed_in():
    result = github_setup.parse_auth_status(_SIGNED_IN_OUTPUT, returncode=0)
    assert result == {"signed_in": True, "account": "octocat"}


def test_parse_auth_status_signed_out():
    result = github_setup.parse_auth_status(_SIGNED_OUT_OUTPUT, returncode=1)
    assert result == {"signed_in": False, "account": None}


def test_parse_auth_status_reports_only_the_github_com_account():
    """gh supports being logged into github.com and a GHE host at once; this
    feature only targets github.com, so that is the one account reported."""
    result = github_setup.parse_auth_status(_MULTI_HOST_OUTPUT, returncode=0)
    assert result == {"signed_in": True, "account": "octocat"}


def test_a_nonzero_exit_is_always_signed_out_even_with_odd_output():
    """UNLIKE claude_health.signed_in, gh's exit code is authoritative — a
    non-zero exit is signed_in=False, never None, regardless of what (if
    anything) came out on stderr."""
    for stderr in ("", "some future message we don't recognise", "error: boom"):
        result = github_setup.parse_auth_status(stderr, returncode=1)
        assert result == {"signed_in": False, "account": None}


def test_a_zero_exit_with_unparseable_output_is_signed_in_with_no_account():
    """A zero exit says gh believes it is signed in even if this parser can't
    find the account line in a future output format — the exit code wins."""
    result = github_setup.parse_auth_status("something new and unexpected",
                                             returncode=0)
    assert result == {"signed_in": True, "account": None}


def test_auth_status_probe_asks_the_cli(monkeypatch):
    _auth_says(monkeypatch, _SIGNED_IN_OUTPUT, returncode=0)
    assert github_setup._auth_status("/x/gh") == {"signed_in": True, "account": "octocat"}

    _auth_says(monkeypatch, _SIGNED_OUT_OUTPUT, returncode=1)
    assert github_setup._auth_status("/x/gh") == {"signed_in": False, "account": None}


def test_auth_status_probe_survives_a_missing_or_hung_binary(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(github_setup.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("gh", 1)))
    assert github_setup._auth_status("/x/gh") is None
    monkeypatch.setattr(github_setup.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert github_setup._auth_status("/x/gh") is None


# -- the subprocess discipline (test_git_posix_spawn.py's pin) ---------------


def test_the_version_probe_never_forks(monkeypatch):
    """close_fds=False is what keeps CPython on posix_spawn — the same
    discipline as every other subprocess in the package (see
    claude_health.SUBPROCESS_KWARGS and test_git_posix_spawn.py)."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        assert os.path.isabs(argv[0])

        class R:
            returncode, stdout, stderr = 0, "gh version 2.63.0", ""
        return R()

    monkeypatch.setattr(github_setup.subprocess, "run", fake_run)
    github_setup.probe_version("/x/gh")
    assert seen["close_fds"] is False
    assert "cwd" not in seen
    assert seen["timeout"] > 0


def test_the_auth_probe_never_forks(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        assert os.path.isabs(argv[0])
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": _SIGNED_IN_OUTPUT})()

    monkeypatch.setattr(github_setup.subprocess, "run", fake_run)
    github_setup._auth_status("/x/gh")
    assert seen["close_fds"] is False
    assert "cwd" not in seen


# -- the cached snapshot ------------------------------------------------------


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    calls = []

    def fake_probe(p):
        calls.append(p)
        return "2.63.0"

    monkeypatch.setattr(github_setup, "probe_version", fake_probe)
    monkeypatch.setattr(github_setup, "_auth_status",
                        lambda path: {"signed_in": True, "account": "octocat"})

    first = github_setup.snapshot()
    assert first["found"] is True and first["version"] == "2.63.0"
    assert os.path.isfile(github_setup._cache_path())

    second = github_setup.snapshot()
    assert second["version"] == "2.63.0"
    assert len(calls) == 1, "a warm cache must not re-probe"


def test_refresh_re_probes_even_on_a_valid_cache(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    calls = []
    monkeypatch.setattr(github_setup, "probe_version",
                        lambda p: calls.append(p) or "2.63.0")
    monkeypatch.setattr(github_setup, "_auth_status",
                        lambda path: {"signed_in": True, "account": "octocat"})
    github_setup.snapshot()
    github_setup.snapshot(refresh=True)
    assert len(calls) == 2


def test_a_missing_binary_is_not_asked_for_its_auth_state(monkeypatch):
    spawned = []
    monkeypatch.setattr(github_setup.subprocess, "run",
                        lambda *a, **k: spawned.append(a) or None)
    monkeypatch.setattr(github_setup, "resolve", lambda: (None, None))
    snap = github_setup._measure()
    assert snap["found"] is False
    assert snap["signed_in"] is False
    assert snap["account"] is None
    assert spawned == []


def test_a_broken_binary_that_wont_report_a_version_is_still_reported(tmp_path, monkeypatch):
    """A resolved, executable-looking file that will not answer `--version` —
    the same 'broken' shape claude_health measures — must not crash the probe;
    it degrades to found=True, version=None, signed_in=False."""
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setattr(github_setup, "probe_version", lambda p: None)
    monkeypatch.setattr(github_setup, "_auth_status", lambda p: None)
    snap = github_setup._measure()
    assert snap["found"] is True
    assert snap["version"] is None
    assert snap["signed_in"] is False
    assert snap["account"] is None


def test_summary_withholds_the_fingerprint(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setattr(github_setup, "probe_version", lambda p: "2.63.0")
    monkeypatch.setattr(github_setup, "_auth_status",
                        lambda path: {"signed_in": True, "account": "octocat"})
    summary = github_setup.summary()
    assert "fingerprint" not in summary
    assert "found" in summary and "path" in summary
    # and it must survive a JSON round trip, being an HTTP payload
    assert json.loads(json.dumps(summary))["found"] is True


# -- the endpoint -------------------------------------------------------------


def _client():
    from starlette.testclient import TestClient

    from fused_render.server.routers.github import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_answers_the_snapshot(monkeypatch):
    monkeypatch.setattr(github_setup, "summary",
                        lambda: {"found": True, "version": "2.63.0"})
    body = _client().get("/api/github/status").json()
    assert body == {"found": True, "version": "2.63.0"}


def test_refresh_requires_the_fused_header(monkeypatch):
    called = []
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: called.append(1) or {"found": False})
    client = _client()
    assert client.post("/api/github/status/refresh").status_code != 200
    assert called == []
    ok = client.post("/api/github/status/refresh", headers={"X-Fused": "1"})
    assert ok.status_code == 200 and called == [1]
