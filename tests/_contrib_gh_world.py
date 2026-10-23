"""A two-person world for the contribution-loop proposal/remix tests: Alice's
repo on a FakeGitHub (real bare repos), Bob's GitHub-link copy of it cloned
into a temp folder with its contrib record, everything git-real.

`https://github.com/<o>/<r>` resolves to the fake's bare repos for EVERY git
process in the test (`FakeGitHub.git_env()` insteadOf), so follow.fetch, a
clone and gh.push all hit the same fake. Nothing here reaches github.com.
"""
from __future__ import annotations

import os
import subprocess
import types

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="fused-app" />
<meta name="fused-api-version" content="1" />
<meta name="fused-app-id" content="sine-starter-0a1b2c3d" />
<title>Sine</title>
</head>
<body>
<p class="wave">blue</p>
</body>
</html>
"""

APP_PY = "def amplitude():\n    return 1\n\n\ndef frequency():\n    return 2\n"


def git(root, *args, env=None, check=True):
    from fused_render.shell import git_tools
    res = subprocess.run([git_tools.git_bin(), "-C", str(root), *args],
                         capture_output=True, text=True, encoding="utf-8",
                         close_fds=False, env={**os.environ, **(env or {})})
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} failed: {res.stderr}")
    return res.stdout.strip()


def claude_commit(root, files: dict, message: str) -> str:
    """What a Claude turn leaves: files edited, one commit as Fused."""
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    git(root, "add", "-A")
    git(root, "-c", "user.name=Fused", "-c", "user.email=apps@fused.io",
        "commit", "-q", "-m", f"Claude: {message}")
    return git(root, "rev-parse", "HEAD")


def edit(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
        fh.write(text)


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


class World(types.SimpleNamespace):
    pass


def make_world(tmp_path, monkeypatch, *, home_branch: bool = True) -> World:
    """`home_branch` (v3, §17.6): Bob's clone is on `fused/mine` like a real
    install leaves it; False keeps the v2 shape (HEAD on main)."""
    from fused_render.contrib import follow, gh, store

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    write_prefs(contrib_enabled=True, contrib_remix_enabled=True)  # §17.9: no flag fakes
    gh.forget_identity()
    monkeypatch.setattr(gh, "_FORK_POLL_STEP_S", 0.01)

    fake.add_user("alice", "Alice")
    fake.add_user("bob", "Bob")
    repo = fake.create_repo_as("alice", "sine", {"index.html": PAGE, "app.py": APP_PY})
    fake.sign_in("bob")

    apps = tmp_path / "apps"
    apps.mkdir()
    app = str(apps / "sine")
    git(apps, "clone", "-q", "https://github.com/alice/sine", app)
    if home_branch:
        on_home_branch(app)
    rec = store.new_record("github", repo="alice/sine", branch="main", classified_by="install")
    store.put(app, rec)
    # The copy id an install's first settle stamps (`classify._stamp_copy`):
    # what keeps the record bound while its origin is re-pointed (§17.6).
    git(app, "config", "--local", "fused.copyid", rec["copy_id"])
    f = follow.for_record(rec)
    got = follow.fetch(app, f)
    assert (got[0] if isinstance(got, tuple) else got), "follow fetch through the fake failed"
    # What an install does (§16.1): never-push guard, Bob's commit identity.
    from fused_render.contrib import classify
    if hasattr(classify, "ensure_push_guard"):
        classify.ensure_push_guard(app, "github")
    if hasattr(classify, "apply_identity"):
        classify.apply_identity(app, block=True)
    return World(fake=fake, repo=repo, app=store.key(app), gh=gh, store=store)


# ------------------------------------------------- v3 (§17) helpers — WS-2


HOME = "fused/mine"


def write_prefs(**values) -> None:
    """Merge `values` into this test's prefs.json (`FUSED_RENDER_HOME` set
    first) — how a test turns the contribution loop on (§17.9), never a
    monkeypatch of `flags`."""
    from fused_render.shell import prefs, storage
    path = prefs._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cur = prefs.read_prefs()
    cur.update(values)
    # Atomic replace, as `prefs.write_pref` does: a new inode every write, so
    # `flags._raw`'s stat memo always sees it (a same-size rewrite too).
    storage.write_json(path, cur)


def on_home_branch(root, branch: str = "main") -> None:
    """What an install does right after the clone (§17.6): HEAD on
    `fused/mine` at the clone's commit, tracking `origin/<branch>`;
    `refs/heads/<branch>` stays the fetch-only mirror."""
    git(root, "switch", "-q", "-c", HOME)
    git(root, "config", f"branch.{HOME}.remote", "origin")
    git(root, "config", f"branch.{HOME}.merge", f"refs/heads/{branch}")


def mirror_main(root, branch: str = "main") -> str:
    """What every sync does after the fetch (I3): `refs/heads/<branch>` set
    to `refs/remotes/origin/<branch>` as a ref update (never checked out).
    Answers the tip."""
    tip = git(root, "rev-parse", f"refs/remotes/origin/{branch}")
    git(root, "update-ref", f"refs/heads/{branch}", tip)
    return tip


def assert_home_invariants(root, branch: str = "main") -> None:
    """I3 + I4 (§17.1): the mirror equals origin's ref and HEAD is
    `fused/mine`."""
    assert git(root, "symbolic-ref", "HEAD") == f"refs/heads/{HOME}", "I4: HEAD off fused/mine"
    assert git(root, "rev-parse", f"refs/heads/{branch}") == \
        git(root, "rev-parse", f"refs/remotes/origin/{branch}"), "I3: mirror != origin"
