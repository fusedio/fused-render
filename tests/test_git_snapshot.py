"""GET /api/git/snapshot — extracting the enclosing app folder at a commit.

Model: a real repository, built with real `git` calls (`tests/test_git_scope.py`'s
fixture style), so the route is exercised against actual git behavior rather than
a mocked one.
"""
import os
import subprocess
import time

import pytest

from fused_render.server.routers import git_snapshot as gs


def _git(*args, cwd, check=True):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, env=env,
                          capture_output=True)


def _commit(cwd, message):
    _git("add", "-A", cwd=cwd)
    _git("-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", message,
        cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with an app folder (`myapp/`) that changes across two commits."""
    root = tmp_path / "repo"
    app_dir = root / "myapp"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>v1</body></html>',
        encoding="utf-8")
    (app_dir / "reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    old_sha = _commit(root, "v1")

    (app_dir / "reader.py").write_text("VALUE = 2\n", encoding="utf-8")
    new_sha = _commit(root, "v2")

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return {"root": root, "app_dir": app_dir, "old_sha": old_sha,
            "new_sha": new_sha}


def _cache_dirs(cache_root):
    if not os.path.isdir(cache_root):
        return []
    out = []
    for key in os.listdir(cache_root):
        key_dir = os.path.join(cache_root, key)
        if os.path.isdir(key_dir):
            out += [os.path.join(key_dir, s) for s in os.listdir(key_dir)]
    return out


# --------------------------------------------------------------- the happy path


def test_extraction_carries_the_old_commits_bytes(repo):
    result = gs.extract_snapshot(
        str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    assert result["ok"] is True
    with open(os.path.join(result["dir"], "reader.py"), encoding="utf-8") as f:
        assert f.read() == "VALUE = 1\n"
    # Not today's content, which the live file already has.
    with open(repo["app_dir"] / "reader.py", encoding="utf-8") as f:
        assert f.read() == "VALUE = 2\n"
    assert result["entry"] == os.path.join(result["dir"], "index.html")
    assert result["app_dir"] == os.path.realpath(str(repo["app_dir"]))


def test_a_second_call_is_a_cache_hit_that_spawns_no_git(repo, monkeypatch):
    first = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"),
                                repo["old_sha"])

    called = []
    real_popen = subprocess.Popen

    def _tracking_popen(*args, **kwargs):
        called.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _tracking_popen)
    second = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"),
                                 repo["old_sha"])
    assert second == first
    assert called == []  # no git, no tar — the cache hit never forks


def test_no_app_above_the_path_is_404(repo):
    plain = repo["root"] / "notes.txt"
    plain.write_text("x", encoding="utf-8")
    _commit(repo["root"], "add notes")
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(plain), repo["old_sha"])
    assert exc.value.status == 404


def test_non_hex_sha_is_400(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), "HEAD~1")
    assert exc.value.status == 400


def test_relative_path_is_400(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot("myapp/reader.py", repo["old_sha"])
    assert exc.value.status == 400


def test_mount_backed_path_refuses(repo, monkeypatch):
    monkeypatch.setattr(
        "fused_render.server.routers.git_snapshot.shell_mounts.is_mount_backed",
        lambda p: True)
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    assert exc.value.status == 400


def test_entry_resolves_from_the_extracted_tree_when_renamed_since(repo):
    """An app whose entry page was renamed since the target commit must open
    at the entry that commit had, not at today's filename."""
    app_dir = repo["app_dir"]
    _git("mv", "index.html", "main.html", cwd=app_dir)
    renamed_sha = _commit(repo["root"], "rename entry")

    result = gs.extract_snapshot(str(app_dir / "main.html"), renamed_sha)
    assert result["entry"] == os.path.join(result["dir"], "main.html")

    # But the OLD commit's extraction still finds the OLD entry name.
    old_result = gs.extract_snapshot(str(app_dir / "reader.py"),
                                     repo["old_sha"])
    assert old_result["entry"] == os.path.join(old_result["dir"], "index.html")


def test_unknown_sha_is_404(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), "f" * 40)
    assert exc.value.status == 404


# ---------------------------------------------------- code review B3 (symlinks)


def test_app_dir_is_reachable_through_a_symlinked_ancestor(repo, tmp_path):
    """Regression for finding B3: a path reaching the repo only through a
    symlinked ancestor (macOS's own /tmp -> /private/tmp is exactly this
    shape) must still resolve — `_repo_root` realpaths, and the old
    `enclosing_app_dir` compared a plain `os.path.abspath` against that
    realpath'd value, so it never matched and returned 404."""
    link = tmp_path / "link_to_repo"
    link.symlink_to(repo["root"])
    result = gs.extract_snapshot(str(link / "myapp" / "reader.py"), repo["old_sha"])
    assert result["ok"] is True
    with open(os.path.join(result["dir"], "reader.py"), encoding="utf-8") as f:
        assert f.read() == "VALUE = 1\n"


def test_app_dir_in_the_response_matches_the_live_path_given(repo, tmp_path):
    """The second half of finding B3: `app_dir` must come back in the SAME
    (possibly symlinked) form the caller's own `path` used, not realpath'd —
    the frontend compares it against the live, non-realpath'd path it already
    holds by plain string prefix (`carries()`/`rewritePath()`), and a
    realpath'd answer would silently fail every one of those comparisons even
    though this route itself reports success."""
    link = tmp_path / "link_to_repo2"
    link.symlink_to(repo["root"])
    result = gs.extract_snapshot(str(link / "myapp" / "reader.py"), repo["old_sha"])
    assert result["app_dir"] == str(link / "myapp")
    assert result["app_dir"] != os.path.realpath(str(link / "myapp"))


# --------------------------------------------------------- code review B7 (gc)


def test_a_cache_hit_bumps_the_trees_mtime(repo):
    """Regression for finding B7: a cache hit must count as a touch for GC
    purposes, or a tree a session is actively re-reading ages exactly like an
    unused one and can be the very first thing a long session's `_gc` reaps —
    an open pane then starts 404ing mid-session with no explanation."""
    first = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    old_mtime = os.stat(first["dir"]).st_mtime
    # Force the clock backward so a bumped mtime is unambiguously later, not
    # merely "close enough that a fast test race could hide a no-op".
    os.utime(first["dir"], (old_mtime - 1000, old_mtime - 1000))
    backdated = os.stat(first["dir"]).st_mtime

    second = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    assert second["dir"] == first["dir"]
    assert os.stat(second["dir"]).st_mtime > backdated


def test_gc_reaps_the_least_recently_touched_tree_not_the_oldest_created(repo):
    """A hit on an OLDER extraction, re-touched, must outlive a NEWER
    extraction nobody has asked for again since — proving `_gc` is an actual
    LRU rather than a first-extracted-first-reaped queue. Calls `_gc`
    directly with an explicit `cap`, rather than monkeypatching
    `MAX_CACHED_SNAPSHOTS` (a module constant baked into `_gc`'s own default
    argument at function-definition time — a monkeypatch of the module
    attribute would never reach a call that relies on that default)."""
    old = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    new = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["new_sha"])
    assert os.path.isdir(old["dir"])
    assert os.path.isdir(new["dir"])

    # Re-touch the OLD one (a hit) and back both up in time so the touch is
    # the only thing keeping `old` younger than `new`.
    os.utime(old["dir"], (0, 0))
    os.utime(new["dir"], (0, 0))
    gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])  # re-touch old
    assert os.stat(new["dir"]).st_mtime == 0  # untouched since the backdate

    gs._gc(gs._cache_root(), cap=1)

    assert os.path.isdir(old["dir"]), "the re-touched (LRU-fresh) tree survived"
    assert not os.path.isdir(new["dir"]), "the untouched tree was reaped instead"


# ------------------------------------------------ GET /api/git/app-folder (B4)


def test_app_folder_probe_finds_the_enclosing_app_with_no_git_fork(repo, monkeypatch):
    called = []
    real_popen = subprocess.Popen

    def _tracking_popen(*args, **kwargs):
        called.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _tracking_popen)
    repo_root, app_dir = gs._resolve_app_dir(str(repo["app_dir"] / "reader.py"))
    assert app_dir == str(repo["app_dir"])
    assert called == []  # a probe is a pure filesystem walk — no git, no tar


def test_app_folder_probe_404s_with_no_enclosing_app(repo):
    plain = repo["root"] / "notes.txt"
    plain.write_text("x", encoding="utf-8")
    _commit(repo["root"], "add notes")
    with pytest.raises(gs._Refused) as exc:
        gs._resolve_app_dir(str(plain))
    assert exc.value.status == 404


# ---------------------------------------------------- code review B6 (deadlock)


def test_a_chatty_git_archive_does_not_deadlock_against_tar(tmp_path, monkeypatch):
    """Regression for finding B6: `git archive`'s stderr used to be read only
    AFTER `tar`'s `communicate()` returned. A `git` that writes more than one
    pipe buffer of stderr before exiting then deadlocks — `tar` blocked
    reading `git`'s stdout, `git` blocked writing its stderr, neither drained
    — and the request stalled for the full `TIMEOUT_S` before reporting a
    bogus "extraction took longer than Ns" (the pipe, not a slow git, was the
    real bottleneck). A fake `git` that writes 2 MB of stderr before emitting
    a minimal valid (empty) tar stream reproduces the shape without needing a
    real chatty repository (huge LFS/filter warning counts, one per file).

    `TIMEOUT_S` is monkeypatched down so the OLD, deadlocking behavior fails
    this test quickly (a `_Refused` 502) rather than hanging for the real 20s
    default; the fix is expected to finish in a small fraction of even that
    shortened bound.
    """
    fake_git = tmp_path / "fake-git.sh"
    fake_git.write_text(
        "#!/bin/sh\n"
        "# Ignores every arg; a stand-in for a `git archive` chattier than\n"
        "# one pipe buffer, terminating in a minimal valid (empty) tar.\n"
        "head -c 2000000 /dev/zero | tr '\\0' 'x' 1>&2\n"
        "dd if=/dev/zero bs=512 count=2 2>/dev/null\n"
        "exit 0\n",
        encoding="utf-8")
    fake_git.chmod(0o755)

    monkeypatch.setattr(gs, "_GIT_BIN", str(fake_git))
    monkeypatch.setattr(gs, "TIMEOUT_S", 5.0)

    dest_tmp = tmp_path / "dest"
    dest_tmp.mkdir()
    started = time.monotonic()
    gs._run_archive(str(tmp_path), "deadbeef", "", str(dest_tmp))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (
        f"took {elapsed:.1f}s — the chatty stderr blocked the pipeline "
        "instead of being drained concurrently"
    )
