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


# ---------------------------------------------- stage B (§18.4) helpers — WS-ENGINE


def assert_market_invariants(root, branch: str = "main") -> None:
    """I3 + I4 read for a `marketplace` copy (§18.4): HEAD on `fused/mine`,
    tracking the MIRROR (`branch.fused/mine.remote = .`), and the mirror ==
    the record's approved commit — never the author's tip."""
    from fused_render.contrib import store
    rec = store.get(root) or {}
    assert rec.get("kind") == "marketplace"
    assert git(root, "symbolic-ref", "HEAD") == f"refs/heads/{HOME}", "I4: HEAD off fused/mine"
    assert git(root, "config", f"branch.{HOME}.remote") == ".", "upstream is not the mirror"
    approved = (rec.get("approved") or {}).get("commit")
    assert approved and git(root, "rev-parse", f"refs/heads/{branch}") == approved, \
        "I3 (marketplace): mirror != approved commit"


def assert_copy_invariants(root, branch: str = "main") -> None:
    """The kind's reading of I3 + I4: a github copy's mirror is origin's,
    a marketplace copy's the approved commit."""
    from fused_render.contrib import classify
    kind = classify.kind_of(root)
    if kind == "marketplace":
        assert_market_invariants(root, branch)
    elif kind == "github":
        assert_home_invariants(root, branch)


class Catalog:
    """The backend's catalog and block list for one test (§18.3 rows):
    `marketplace.cached_snapshot/get_snapshot/_read_cache/blocklist`
    answer from here — `status`/`stale` steer the freshness rule, `down`
    makes every read fail (offline)."""

    def __init__(self, tmp_path):
        self.rows: dict = {}
        self.blocked: list = []
        self.status = "ok"
        self.stale = False
        self.down = False
        self.state = str(tmp_path / "market-state")
        os.makedirs(self.state, exist_ok=True)

    def snap(self):
        import copy
        if self.down:
            return None
        return {"status": self.status, "stale": self.stale, "apps": copy.deepcopy(self.rows)}

    def list_app(self, slug, repo, commit, *, whats_new="First release", at="2026-10-01T10:00:00Z",
                 branch="main", version="1.0.0"):
        self.rows[slug] = {"slug": slug, "repo": repo, "branch": branch, "commit": commit,
                           "name": slug, "author": {"name": repo.split("/")[0]},
                           "version": version,
                           "releases": [{"commit": commit, "at": at, "whats_new": whats_new,
                                         "version": version, "blocked": None}]}
        return self.rows[slug]

    def release(self, slug, commit, *, whats_new="Better", at="2026-10-20T10:00:00Z",
                credits=None, version="1.1.0"):
        row = self.rows[slug]
        row["commit"] = commit
        row["version"] = version
        rel = {"commit": commit, "at": at, "whats_new": whats_new, "version": version,
               "blocked": None}
        if credits is not None:
            rel["credits"] = credits
        row["releases"].insert(0, rel)
        return row

    def delist(self, slug):
        self.rows.pop(slug, None)

    def block(self, slug, commit, level="hard", reason="It read saved passwords",
              what_to_do="Change your passwords."):
        self.blocked.append({"slug": slug, "commit": commit, "level": level, "reason": reason,
                             "what_to_do": what_to_do, "at": "2026-10-25T10:00:00Z"})
        for r in (self.rows.get(slug) or {}).get("releases") or []:
            if r["commit"] == commit:
                r["blocked"] = level

    def install(self, monkeypatch):
        import time as _time

        from fused_render import marketplace
        monkeypatch.setattr(marketplace, "cached_snapshot", lambda: (self.snap(), None),
                            raising=False)
        monkeypatch.setattr(marketplace, "get_snapshot",
                            lambda refresh=False, force=False: (self.snap(), None), raising=False)
        monkeypatch.setattr(marketplace, "_read_cache", lambda: self.snap(), raising=False)
        monkeypatch.setattr(marketplace, "state_dir", lambda: self.state, raising=False)

        def blocklist(force=False):
            if self.down:
                raise OSError("offline")
            return {"blocked": list(self.blocked), "checked_at": _time.time()}
        monkeypatch.setattr(marketplace, "blocklist", blocklist, raising=False)
        return self


def make_market_copy(w, cat, slug="sine", commit=None):
    """Turn the world's GitHub-link copy into what a Marketplace install
    leaves (§18.4): kind `marketplace`, the row's approved commit, HEAD and
    the mirror at it, upstream = the mirror, marker `marketplace`."""
    from fused_render.contrib import classify, replay, store
    app = w.app
    commit = commit or git(app, "rev-parse", "refs/remotes/origin/main")
    if slug not in cat.rows:
        cat.list_app(slug, "alice/sine", commit)
    git(app, "reset", "-q", "--hard", commit)
    git(app, "update-ref", "refs/heads/main", commit)
    rec = store.get(app)
    rec.update(kind="marketplace", owner_checked=True,
               **store.market_fields({"slug": slug, "approved": cat.rows[slug]["releases"][0]}))
    store.put(app, rec)
    classify._forget_kind(app)
    with replay.slot() as held:
        assert held
        classify.settle_locked(app)
    return commit
