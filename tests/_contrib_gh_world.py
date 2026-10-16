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
import sys
import time
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


def make_world(tmp_path, monkeypatch) -> World:
    from fused_render.contrib import flags, follow, gh, store

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(flags, "_raw", lambda: dict.fromkeys(
        ("sources", "changes", "inbox", "loop", "remix"), True))
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
    rec = store.new_record("github", repo="alice/sine", branch="main", classified_by="install")
    store.put(app, rec)
    f = follow.for_record(rec)
    assert follow.fetch(app, f), "follow fetch through the fake failed"
    from fused_render.contrib import replay
    git(app, "update-ref", replay.BASE_REF, follow.tip(app, f))
    return World(fake=fake, repo=repo, app=store.key(app), gh=gh, store=store)


def install_engine_stub(monkeypatch):
    """A minimal stand-in for WS-A's `engine.return_to_following`, enough to
    exercise the A4 trigger: replay `proposed_head..HEAD` onto the follow tip
    (unproposed edits kept), land, mark `returned_at`."""
    from fused_render.contrib import follow, replay, store

    calls = []

    def return_to_following(root):
        calls.append(root)
        rec = store.get(root)
        p = rec["proposal"]
        f = follow.for_record(rec)
        tip = follow.tip(root, f)
        head = replay.rev(root, "HEAD")
        res = replay.replay(root, onto=tip, upstream=p["proposed_head"], branch="main")
        assert res["ok"], res
        assert replay.land(root, branch="main", old_tip=head, new_tip=res["tip"], why="return")
        store.update(root, lambda r: r["proposal"].update(returned_at=time.time()))
        return {"ok": True}

    mod = types.ModuleType("fused_render.contrib.engine")
    mod.return_to_following = return_to_following
    monkeypatch.setitem(sys.modules, "fused_render.contrib.engine", mod)
    import fused_render.contrib as pkg
    monkeypatch.setattr(pkg, "engine", mod, raising=False)
    return calls
