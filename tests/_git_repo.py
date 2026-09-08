"""Throwaway git repositories for the `git` template's tests (SPEC §33).

Both suites — the condition gate (tests/test_git_condition.py) and the reader
(tests/test_git_reader.py) — need a REAL repository: the whole point of the
template is that `git` itself is the authority on what a repository is and what
happened in it, so a mocked `subprocess` would test our own fiction. These
helpers build one with a fixed shape and deterministic timestamps.

Every invocation is fully self-contained: `-c` flags for identity and default
branch, and `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` pointed at nowhere, so a
developer's own `~/.gitconfig` (aliases, `commit.gpgsign`, a `log.date`
override, hooks via `core.hooksPath`) cannot change the fixture's history.

`git_available()` lets a suite skip rather than fail on a machine with no git —
the reader and the gate both degrade to an empty state there, which is covered
by its own test instead.
"""
import os
import subprocess

# Fixed identity + dates, so a subject/author/date assertion is stable and the
# commit shas are reproducible for a given tree.
_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}

_IDENTITY = (
    "-c", "user.name=Fixture Author",
    "-c", "user.email=fixture@example.com",
    "-c", "init.defaultBranch=main",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "gc.auto=0",
)


def git(cwd, *args, when=None, check=True):
    """Run one git command inside `cwd`; returns its stdout as text."""
    env = {**os.environ, **_ENV}
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8", "replace")


def git_available():
    try:
        subprocess.run(
            ["git", "--version"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def empty_repo(root):
    """An initialized repository with NO commits yet — the `git init` state."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    return root


def write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    return full


def bare_repo(path):
    """A bare repository, i.e. something a clone can legitimately push to.

    The write ops (fetch/pull/push, SPEC GT-12) are only meaningfully testable
    against a REAL remote — a mocked one would test our fiction of git's
    transport rather than the transport. A bare repo in a tmpdir is the cheapest
    real remote there is: a local path, no network, no credentials, so
    `GIT_TERMINAL_PROMPT=0` never has anything to prompt about.
    """
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "--bare")
    return path


def with_remote(root, remote_path, *, push=True):
    """Point `root` at a fresh bare `origin` and (by default) publish HEAD.

    `-u` on the push is what records the UPSTREAM, which is the fact the reader
    reports ahead/behind against — and it must be recorded rather than fetched,
    because a read may never contact a remote as a side effect (GT-12).
    """
    bare_repo(remote_path)
    git(root, "remote", "add", "origin", remote_path)
    if push:
        git(root, "push", "-q", "-u", "origin", "HEAD")
    return remote_path


def build_repo(root):
    """A repository with history worth paging, renames, a binary blob, and a
    dirty working tree (staged + unstaged + untracked).

    Shape (oldest first), so a scoped-log test has something to scope OUT:

      c1  "add readme"            README.md
      c2  "add the module"        pkg/mod.py, pkg/notes.md
      c3  "edit the module"       pkg/mod.py
      c4  "rename the module"     pkg/mod.py -> pkg/core.py
      c5  "add a logo"            assets/logo.bin  (binary)
      c6  "unrelated top change"  README.md

    Working tree on top of c6:
      pkg/core.py      modified, unstaged
      pkg/staged.txt   added, staged
      pkg/fresh.txt    untracked
      README.md        modified, unstaged (OUTSIDE pkg/ — scope check)
    """
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")

    write(root, "README.md", "# fixture\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add readme", when="2026-01-01T10:00:00+00:00")

    write(root, "pkg/mod.py", "def one():\n    return 1\n")
    write(root, "pkg/notes.md", "notes\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add the module", when="2026-01-02T10:00:00+00:00")

    write(root, "pkg/mod.py", "def one():\n    return 11\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "edit the module", when="2026-01-03T10:00:00+00:00")

    git(root, "mv", os.path.join("pkg", "mod.py"), os.path.join("pkg", "core.py"))
    git(root, "commit", "-q", "-m", "rename the module", when="2026-01-04T10:00:00+00:00")

    os.makedirs(os.path.join(root, "assets"), exist_ok=True)
    with open(os.path.join(root, "assets", "logo.bin"), "wb") as handle:
        handle.write(bytes(range(256)) * 4)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add a logo", when="2026-01-05T10:00:00+00:00")

    write(root, "README.md", "# fixture\n\nnow with prose\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "unrelated top change",
        when="2026-01-06T10:00:00+00:00")

    # Dirty working tree, deliberately spanning the scope boundary.
    write(root, "pkg/core.py", "def one():\n    return 111\n")
    write(root, "pkg/staged.txt", "staged\n")
    git(root, "add", os.path.join("pkg", "staged.txt"))
    write(root, "pkg/fresh.txt", "brand new\n")
    write(root, "README.md", "# fixture\n\nnow with more prose\n")
    return root
