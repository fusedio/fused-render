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
import shutil
import sys
import threading

import pytest

from fused_render import github_setup

# Captured before any test can monkeypatch `github_setup.threading.Thread` —
# that attribute lives on the SAME module object this file drives its own
# concurrency tests with, so a fake installed there would swallow the test's
# own driver threads too (see test_claude_install.py's identical capture).
_RealThread = threading.Thread


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


# -- installing `gh` ----------------------------------------------------------
#
# Ported from tests/test_claude_install.py's shape: nothing below spawns a
# real download. `github_setup._download` is faked at the module boundary and
# the health re-probe is stubbed, so what is under test is the state machine
# and the URL/asset-name arithmetic, not the network.


@pytest.fixture(autouse=True)
def _clean_install(monkeypatch):
    github_setup.install_reset()
    monkeypatch.setattr(github_setup.jobs, "upsert", lambda *a, **k: {})
    yield
    github_setup.install_reset()


# -- the asset-name / URL arithmetic, pure and offline ------------------------


@pytest.mark.parametrize("os_kind,arch,ext", [
    ("macOS", "amd64", "zip"),
    ("macOS", "arm64", "zip"),
    ("linux", "amd64", "tar.gz"),
    ("linux", "arm64", "tar.gz"),
    ("windows", "amd64", "zip"),
    ("windows", "arm64", "zip"),
])
def test_asset_name_matches_the_current_release_shape(os_kind, arch, ext):
    """Checked live against api.github.com/repos/cli/cli/releases/latest on
    2.100.0: every one of these six names is a real asset on that release."""
    name = github_setup._asset_name(os_kind, arch, "2.100.0")
    assert name == f"gh_2.100.0_{os_kind}_{arch}.{ext}"


@pytest.mark.parametrize("os_kind,arch", [
    ("macOS", "amd64"), ("macOS", "arm64"),
    ("linux", "amd64"), ("linux", "arm64"),
    ("windows", "amd64"), ("windows", "arm64"),
])
def test_release_url_points_at_the_tagged_release(os_kind, arch):
    """NOT `/releases/latest/download/...`: that redirect only works when the
    requested filename already exists among the latest release's assets, and
    every `gh` asset name embeds its version — a filename built without
    knowing the version 404s (checked live: a `gh_1.0.0_...` name against
    `/latest/download/` on the real repo comes back 404, not a redirect).
    So the version is resolved first (`_fetch_latest_version`) and the URL is
    built against the tag it names.
    """
    asset = github_setup._asset_name(os_kind, arch, "2.100.0")
    url = github_setup._release_url("2.100.0", asset)
    assert url == f"https://github.com/cli/cli/releases/download/v2.100.0/{asset}"


def test_target_os_reads_sys_platform_and_os_name(monkeypatch):
    monkeypatch.setattr(github_setup.sys, "platform", "darwin")
    assert github_setup._target_os() == "macOS"
    monkeypatch.setattr(github_setup.sys, "platform", "linux")
    monkeypatch.setattr(github_setup.os, "name", "nt")
    assert github_setup._target_os() == "windows"
    monkeypatch.setattr(github_setup.os, "name", "posix")
    assert github_setup._target_os() == "linux"


@pytest.mark.parametrize("machine,want", [
    ("x86_64", "amd64"), ("AMD64", "amd64"),
    ("arm64", "arm64"), ("aarch64", "arm64"),
])
def test_target_arch_normalizes_platform_machine(machine, want):
    assert github_setup._target_arch(machine) == want


def test_target_arch_refuses_an_unpublished_architecture():
    with pytest.raises(github_setup.InstallError, match="architecture"):
        github_setup._target_arch("i386")


def test_member_for_names_the_binary_inside_the_archive():
    assert (github_setup._member_for("linux", "amd64", "2.100.0")
            == "gh_2.100.0_linux_amd64/bin/gh")
    assert (github_setup._member_for("windows", "amd64", "2.100.0")
            == "gh_2.100.0_windows_amd64/bin/gh.exe")


# -- the install worker --------------------------------------------------------


def _run_install(monkeypatch, *, version="2.100.0", download_error=None,
                 health=None):
    """Drive one install to completion synchronously, returning the record."""
    monkeypatch.setattr(github_setup, "_fetch_latest_version", lambda: version)

    def fake_download(url, dest_path):
        if download_error is not None:
            raise download_error
        os_kind = github_setup._target_os()
        arch = github_setup._target_arch(github_setup.platform.machine())
        member = github_setup._member_for(os_kind, arch, version)
        # Build a tiny real archive of the right shape so `_unpack` (not
        # faked) has something genuine to extract from.
        root = member.rsplit("/", 2)[0]
        if dest_path.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(dest_path, "w") as zf:
                zf.writestr(member, "#!/bin/sh\necho fake gh\n")
                zf.writestr(f"{root}/LICENSE", "MIT")
        else:
            import io
            import tarfile
            with tarfile.open(dest_path, "w:gz") as tf:
                data = b"#!/bin/sh\necho fake gh\n"
                info = tarfile.TarInfo(name=member)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(github_setup, "_download", fake_download)
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: health if health is not None
                        else {"found": True, "version": version})
    github_setup._run_install()
    return github_setup.install_status()


def test_a_clean_install_finishes_and_installs_the_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    rec = _run_install(monkeypatch)
    assert rec["state"] == "done"
    assert rec["error"] is None
    dest = os.path.join(github_setup.install_dir(), github_setup._binary_name())
    assert os.path.isfile(dest)
    if os.name != "nt":
        assert os.access(dest, os.X_OK)


def test_a_failing_download_surfaces_its_real_error(tmp_path, monkeypatch):
    """A 403 from GitHub and a proxy eating the TLS handshake are different
    documented problems with different fixes — a reworded "install failed"
    would throw both away."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    rec = _run_install(monkeypatch,
                       download_error=OSError("[Errno 403] Forbidden"))
    assert rec["state"] == "error"
    assert "403" in rec["error"]


def test_success_re_probes_and_the_snapshot_flips_to_found(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    probed = []

    def fake_refreshed():
        probed.append(1)
        return {"found": True, "version": "2.100.0"}

    monkeypatch.setattr(github_setup, "summary_refreshed", fake_refreshed)
    monkeypatch.setattr(github_setup, "_fetch_latest_version", lambda: "2.100.0")

    def fake_download(url, dest_path):
        os_kind = github_setup._target_os()
        arch = github_setup._target_arch(github_setup.platform.machine())
        member = github_setup._member_for(os_kind, arch, "2.100.0")
        import io
        import tarfile
        with tarfile.open(dest_path, "w:gz") as tf:
            data = b"#!/bin/sh\necho fake gh\n"
            info = tarfile.TarInfo(name=member)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(github_setup, "_target_os", lambda: "linux")
    monkeypatch.setattr(github_setup, "_download", fake_download)
    github_setup._run_install()
    rec = github_setup.install_status()
    assert rec["state"] == "done"
    assert probed == [1]


def test_an_install_that_leaves_nothing_runnable_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    rec = _run_install(monkeypatch, health={"found": False, "version": None})
    assert rec["state"] == "error"
    assert "still cannot be found" in rec["error"]


def test_a_second_install_is_refused_rather_than_queued(monkeypatch):
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: None})())
    github_setup.install_start()
    with pytest.raises(github_setup.InstallError, match="already running"):
        github_setup.install_start()


def test_two_concurrent_starts_only_ever_launch_one_worker(monkeypatch):
    spawned = []
    at_the_gate = threading.Barrier(2, timeout=30)

    def _slow_fetch_version():
        at_the_gate.wait()
        return "2.100.0"

    monkeypatch.setattr(github_setup, "_fetch_latest_version", _slow_fetch_version)
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda self: spawned.append(kw)})())

    refused = []

    def _go():
        try:
            github_setup.install_start()
        except github_setup.InstallError:
            refused.append(1)

    threads = [_RealThread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(spawned) == 1
    assert len(refused) == 1


def test_a_worker_that_dies_unexpectedly_frees_the_slot(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda self: captured.setdefault(
                                "target", kw["target"])})())

    def _explode():
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(github_setup, "_run_install", _explode)
    github_setup.install_start()
    assert github_setup.install_running() is True  # claimed
    captured["target"]()                            # the worker body dies
    rec = github_setup.install_status()
    assert rec["state"] == "error"
    assert "stopped unexpectedly" in rec["error"]
    assert github_setup.install_running() is False


# -- the install endpoints -----------------------------------------------------


def test_install_endpoint_refuses_a_blind_cross_origin_post():
    resp = _client().post("/api/github/install")
    assert resp.status_code in (400, 403)


def test_install_status_endpoint_is_a_read_and_needs_no_guard():
    resp = _client().get("/api/github/install")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_install_endpoint_starts_the_worker(monkeypatch):
    # `_run_install` is stubbed rather than `threading.Thread` itself: the
    # TestClient's own transport spins up a real anyio worker thread to run
    # this request, and replacing the process-wide `Thread` class out from
    # under it (not just this module's work) would break that transport too
    # — a real thread still gets spawned here, it just does nothing.
    monkeypatch.setattr(github_setup, "_run_install", lambda: None)
    resp = _client().post("/api/github/install", headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_a_second_install_via_the_endpoint_comes_back_as_409(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _slow_run():
        started.set()
        release.wait(10)

    monkeypatch.setattr(github_setup, "_run_install", _slow_run)
    client = _client()
    client.post("/api/github/install", headers={"X-Fused": "1"})
    assert started.wait(10), "the worker never started"
    resp = client.post("/api/github/install", headers={"X-Fused": "1"})
    release.set()
    assert resp.status_code == 409


# -- creating the repo and pushing ---------------------------------------------
#
# Ported from the install tests' shape, plus a couple of pytest.ini's tests
# use REAL repositories (tests/_git_repo.py) rather than a mocked git: the
# refusals under test — no commits, an existing remote — are exactly the
# facts git itself is the authority on, so a fake would test our own
# fiction of them instead. `gh` itself is always faked: nothing here talks
# to github.com.

from _git_repo import empty_repo, git, git_available, with_remote  # noqa: E402

# Captured at import time, before `_isolated_home` strips PATH down to an
# empty directory for every test in this file (that stripping is what makes
# `gh` resolution tests trustworthy — see that fixture's own docstring). The
# publish tests below need a REAL `git` regardless, so this is put back on
# PATH just for them; `gh` resolution in these tests is always stubbed
# directly (`monkeypatch.setattr(github_setup, "resolve", ...)`) rather than
# left to find anything on PATH, so restoring it here does not weaken what
# the isolation above is for.
_REAL_GIT_DIR = os.path.dirname(shutil.which("git") or "")


@pytest.fixture(autouse=True)
def _clean_publish(monkeypatch):
    github_setup.publish_reset()
    monkeypatch.setattr(github_setup.jobs, "upsert", lambda *a, **k: {})
    yield
    github_setup.publish_reset()


def _use_real_git(monkeypatch):
    """Puts the real `git` back on PATH for a test that needs one of
    tests/_git_repo.py's real fixtures — undoing, for THIS test alone,
    `_isolated_home`'s blanket PATH strip. Restoring it file-wide instead
    (an autouse fixture touching PATH for every test here) would put
    whatever real `gh` this machine happens to have back within
    `resolve()`'s reach too, and quietly break the `gh`-resolution tests'
    own isolation — which is the entire reason `_isolated_home` strips PATH
    in the first place. `gh` resolution in every test that calls this is
    always stubbed directly anyway (`monkeypatch.setattr(github_setup,
    "resolve", ...)`), so nothing here depends on PATH finding `gh`.
    """
    if _REAL_GIT_DIR:
        monkeypatch.setenv("PATH", os.pathsep.join(
            [_REAL_GIT_DIR, os.environ.get("PATH", "")]))


def _repo_with_a_commit(tmp_path, monkeypatch):
    _use_real_git(monkeypatch)
    if not git_available():
        pytest.skip("git is not available")
    root = tmp_path / "repo"
    empty_repo(str(root))
    (root / "a.txt").write_text("hello\n")
    git(str(root), "add", "a.txt")
    git(str(root), "commit", "-q", "-m", "first")
    return str(root)


def test_validate_repo_name_refuses_empty_and_dash_leading():
    assert github_setup._validate_repo_name("my-repo") == "my-repo"
    with pytest.raises(github_setup.PublishError, match="required"):
        github_setup._validate_repo_name("")
    with pytest.raises(github_setup.PublishError, match="dash"):
        github_setup._validate_repo_name("--upload-pack=/bin/sh")


def test_has_commits_and_has_remote_read_a_real_repo(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    assert github_setup._has_commits(root) is True
    assert github_setup._has_remote(root) is False

    remote = str(tmp_path / "remote.git")
    with_remote(root, remote, push=False)
    assert github_setup._has_remote(root) is True


def test_an_empty_repository_has_no_commits(tmp_path, monkeypatch):
    _use_real_git(monkeypatch)
    if not git_available():
        pytest.skip("git is not available")
    root = tmp_path / "empty"
    empty_repo(str(root))
    assert github_setup._has_commits(str(root)) is False


def test_publish_refuses_before_spawning_when_there_are_no_commits(tmp_path, monkeypatch):
    _use_real_git(monkeypatch)
    if not git_available():
        pytest.skip("git is not available")
    root = tmp_path / "empty"
    empty_repo(str(root))
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    with pytest.raises(github_setup.PublishError, match="no commits"):
        github_setup.publish_start(str(root), "my-repo", "private")


def test_publish_refuses_before_spawning_when_a_remote_already_exists(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    git(root, "remote", "add", "origin", "https://example.invalid/x.git")
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    with pytest.raises(github_setup.PublishError, match="already has a remote"):
        github_setup.publish_start(root, "my-repo", "private")


def test_publish_refuses_a_mount_backed_repo(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    monkeypatch.setattr(github_setup.shell_mounts, "is_mount_backed", lambda p: True)
    with pytest.raises(github_setup.PublishError, match="remote mounts"):
        github_setup.publish_start(root, "my-repo", "private")


def test_publish_refuses_without_gh(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup, "resolve", lambda: (None, None))
    with pytest.raises(github_setup.PublishError, match="GitHub CLI"):
        github_setup.publish_start(root, "my-repo", "private")


def test_publish_refuses_without_an_explicit_visibility(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    for bad in (None, "", "public-ish"):
        with pytest.raises(github_setup.PublishError, match="visibility"):
            github_setup.publish_start(root, "my-repo", bad)


def test_visibility_is_always_explicit_in_the_argv(tmp_path, monkeypatch):
    """The argv never defaults to public or private on its own — whichever
    the caller named is the only flag that appears."""
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "https://github.com/x/y\n",
                              "stderr": ""})()

    monkeypatch.setattr(github_setup, "_spawn_gh_repo_create", fake_run)
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: kw["target"]()})())
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)

    github_setup.publish_start(root, "my-repo", "private")
    assert "--private" in captured["cmd"]
    assert "--public" not in captured["cmd"]

    github_setup.publish_reset()
    github_setup.publish_start(root, "my-repo", "public")
    assert "--public" in captured["cmd"]
    assert "--private" not in captured["cmd"]


def test_a_name_from_the_page_cannot_inject_an_argument(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    with pytest.raises(github_setup.PublishError, match="dash"):
        github_setup.publish_start(root, "--upload-pack=/bin/sh", "private")


def test_ghs_stderr_reaches_the_record_verbatim(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)

    def fake_run(cmd):
        return type("R", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "GraphQL: Name already exists on this account (createRepository)",
        })()

    monkeypatch.setattr(github_setup, "_spawn_gh_repo_create", fake_run)
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: kw["target"]()})())
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)

    github_setup.publish_start(root, "my-repo", "private")
    rec = github_setup.publish_status()
    assert rec["state"] == "error"
    assert "already exists" in rec["error"]


def test_a_successful_publish_records_the_repo_url(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)

    def fake_run(cmd):
        return type("R", (), {"returncode": 0,
                              "stdout": "https://github.com/octocat/my-repo\n",
                              "stderr": ""})()

    monkeypatch.setattr(github_setup, "_spawn_gh_repo_create", fake_run)
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: kw["target"]()})())
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)

    github_setup.publish_start(root, "my-repo", "public")
    rec = github_setup.publish_status()
    assert rec["state"] == "done"
    assert rec["detail"] == "https://github.com/octocat/my-repo"


def test_a_second_publish_is_refused_rather_than_queued(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    github_setup.publish_start(root, "my-repo", "private")
    with pytest.raises(github_setup.PublishError, match="already running"):
        github_setup.publish_start(root, "another-repo", "private")


def test_a_publish_worker_that_dies_unexpectedly_frees_the_slot(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(github_setup.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda self: captured.setdefault(
                                "target", kw["target"])})())
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)

    def _explode(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(github_setup, "_run_publish", _explode)
    github_setup.publish_start(root, "my-repo", "private")
    assert github_setup.publish_running() is True  # claimed
    captured["target"]()                            # the worker body dies
    rec = github_setup.publish_status()
    assert rec["state"] == "error"
    assert "stopped unexpectedly" in rec["error"]
    assert github_setup.publish_running() is False


# -- the publish endpoints ------------------------------------------------------


def test_publish_endpoint_refuses_a_blind_cross_origin_post():
    resp = _client().post("/api/github/publish",
                          json={"root": "/tmp/whatever", "name": "x",
                                "visibility": "private"})
    assert resp.status_code in (400, 403)


def test_publish_status_endpoint_is_a_read_and_needs_no_guard():
    resp = _client().get("/api/github/publish")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_publish_endpoint_starts_the_worker(tmp_path, monkeypatch):
    root = _repo_with_a_commit(tmp_path, monkeypatch)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    monkeypatch.setattr(github_setup, "_run_publish", lambda *a, **k: None)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": root, "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_publish_endpoint_refuses_a_root_outside_any_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": str(tmp_path), "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 409
    assert "not inside a git repository" in resp.json().get("error", "")
