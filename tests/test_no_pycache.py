"""fused-render leaves no `__pycache__` in a user's app folder — and never lets
one distort the listing.

The app folder is the user's: they edit it, commit it, and see it in Recent.
CPython caches bytecode next to the source it imports, so both execution paths
were writing into that folder on every run — the built-in worker for the page's
own `.py` AND its siblings, the fused engine for the siblings. The damage was
not the disk space: `git add -A` swept the .pyc files into app history, and the
directory's mtime moved on every run, so merely OPENING a page made it outrank
one that had actually been edited.

Four layers, one per test group below: it is not created (both engines), it is
not committed (and already-committed ones are untracked once), and it does not
count as "touched" for recency. The fourth — search pruning via
`walk.WALK_IGNORE_DIRS`, SPEC SR-2a — predates this and has its own tests.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from fused_render import app_git, app_listing, engine

CHILD = os.path.join(os.path.dirname(os.path.abspath(app_git.__file__)), "_child.py")


def _app_folder(tmp_path):
    """A page whose .py imports a sibling — the shape that cached TWO .pyc files.

    The sibling matters: `sys.path` gets the app dir, so an import from user code
    resolves there and caches there. A page with no imports would pass a weaker
    version of this test without exercising that path at all.
    """
    (tmp_path / "helper.py").write_text("VALUE = 41\n", encoding="utf-8")
    (tmp_path / "compute.py").write_text(
        "from helper import VALUE\n"
        "def main():\n"
        "    return VALUE + 1\n",
        encoding="utf-8")
    return tmp_path / "compute.py"


def _pycache_dirs(root) -> list[str]:
    return [os.path.join(dirpath, d)
            for dirpath, dirnames, _ in os.walk(root)
            for d in dirnames if d == "__pycache__"]


# ----------------------------------------------------------- not created

def test_the_worker_leaves_no_pycache_in_the_app_folder(tmp_path):
    """Driven through the REAL `_child.py`, because the bug was in how it loads.

    `spec_from_file_location` + `exec_module` is a SourceFileLoader, which caches
    bytecode beside the source; asserting on `sys.dont_write_bytecode` being set
    somewhere would restate the fix instead of testing it. The result assertion
    is not decoration — it proves the sibling import actually ran, so a future
    change that breaks importing cannot make this test pass vacuously.
    """
    page = _app_folder(tmp_path)
    out = subprocess.run(
        [sys.executable, CHILD],
        input=json.dumps({"path": str(page), "params": {}}),
        capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    body = json.loads(out.stdout)
    assert body["ok"] is True, body.get("error")
    assert body["result"] == 42, "the sibling import must really have run"
    assert _pycache_dirs(tmp_path) == [], \
        "the worker wrote bytecode into the user's app folder"


def test_the_engine_wrapper_disables_bytecode_before_extending_sys_path(tmp_path):
    """The fused engine's generated wrapper, executed for real.

    Its preamble is what puts the app dir on sys.path, so the flag has to be set
    ABOVE that line — after it, the first sibling import has already cached. The
    ordering assertion pins that; running the preamble proves it works. Only the
    preamble is executed: the epilogue reaches for `fused` and `_params.json`,
    neither of which this test needs to know about, and caching happens entirely
    within the user code the preamble exec's.
    """
    page = _app_folder(tmp_path)
    user_code = page.read_text(encoding="utf-8")
    code = engine.build_code(user_code, str(tmp_path), str(page))

    flag = code.index("dont_write_bytecode")
    assert flag < code.index("path.insert"), \
        "bytecode must be disabled before the app dir joins sys.path"

    preamble = code[:code.index("exec(compile(")] + code[
        code.index("exec(compile("):code.index("\n", code.index("exec(compile("))]
    out = subprocess.run([sys.executable, "-c", preamble],
                         capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert _pycache_dirs(tmp_path) == [], \
        "the engine wrapper cached the page's sibling imports into the app folder"


# ------------------------------------------------------------ not committed

def _git_ok() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_git = pytest.mark.skipif(not _git_ok(), reason="git not available")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """`commit()` resolves the app repo through `app_dir_for`, which only
    recognises `<fused_dir()>/<tag>/<name>` — an app built anywhere else is a
    silent no-op, so these tests have to use the real layout."""
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


def _app(workspace, name="demo"):
    app = workspace / "local" / name
    app.mkdir(parents=True)
    (app / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    return app


@needs_git
def test_a_new_app_ignores_pycache_from_its_first_commit(workspace):
    app = _app(workspace)
    assert app_git.init_repo(str(app)) is True
    assert "__pycache__/" in (app / ".gitignore").read_text(encoding="utf-8")

    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "compute.cpython-311.pyc").write_bytes(b"\x00")
    (app / "index.html").write_text("<h1>edited</h1>", encoding="utf-8")
    assert app_git.commit(str(app / "index.html"), "second") is True

    assert _tracked(app) == [".gitignore", "index.html"]


@needs_git
def test_an_old_app_untracks_the_pycache_it_already_committed(workspace):
    """The migration path, and the reason an ignore rule is not enough on its own.

    An app that ran Python before this change has .pyc blobs IN THE INDEX, and
    git keeps tracking a path it already tracks no matter what the ignore rules
    say. `_ensure_excludes` untracks them once, on the commit that first appends
    the pattern. Nested as well as top-level — a page importing from a subpackage
    caches there too.
    """
    app = _app(workspace)
    (app / "sub" / "__pycache__").mkdir(parents=True)
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "compute.cpython-311.pyc").write_bytes(b"\x00")
    (app / "sub" / "__pycache__" / "helper.cpython-311.pyc").write_bytes(b"\x00")

    # A repo as it existed BEFORE the pattern — committed .pyc files and an
    # exclude file that never mentioned them.
    subprocess.run(["git", "init", "-q", str(app)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(app), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "old"],
                   check=True, timeout=30)
    assert "__pycache__/compute.cpython-311.pyc" in _tracked(app)

    (app / "index.html").write_text("<h1>edited</h1>", encoding="utf-8")
    assert app_git.commit(str(app / "index.html"), "after the upgrade") is True

    assert _tracked(app) == ["index.html"], "the committed .pyc blobs must be gone"
    assert (app / "__pycache__" / "compute.cpython-311.pyc").exists(), \
        "--cached must leave the user's files on disk"
    assert (app / "sub" / "__pycache__" / "helper.cpython-311.pyc").exists()


@needs_git
def test_untracking_runs_once_per_repo_and_never_again(workspace, monkeypatch):
    """The gate is what keeps this from being a git invocation on every commit —
    and from re-deleting a `__pycache__` a user deliberately re-added.

    `init_repo` writes the patterns to the app's `.gitignore`, so the FIRST
    commit is still the one that seeds `.git/info/exclude` and therefore the one
    that untracks. Every commit after it appends nothing and must stay silent.
    """
    app = _app(workspace)
    app_git.init_repo(str(app))

    calls = []
    monkeypatch.setattr(app_git, "_untrack_pycache", calls.append)
    for i in range(3):
        (app / "index.html").write_text(f"<h1>v{i}</h1>", encoding="utf-8")
        assert app_git.commit(str(app / "index.html"), f"edit {i}") is True

    assert calls == [str(app)], "the migration must run once, on the first commit"


@needs_git
def test_ensure_excludes_never_raises_on_a_folder_that_is_not_a_repo(tmp_path):
    """Best-effort is the contract in this module — a plain folder must be a
    no-op, not an exception into the caller's commit path."""
    plain = tmp_path / "plain"
    plain.mkdir()
    app_git._ensure_excludes(str(plain))  # must not raise


def _tracked(app) -> list[str]:
    out = subprocess.run(["git", "-C", str(app), "ls-files"],
                         capture_output=True, text=True, timeout=30)
    return sorted(p for p in out.stdout.split("\n") if p)


# --------------------------------------------------------- not "recent"

def test_a_pycache_does_not_make_an_app_look_recently_edited(tmp_path):
    """The symptom that made this visible: an app you only OPENED jumping over
    one you actually edited.

    A `__pycache__` written now, against sources edited a day ago. Before the
    fix `dir_updated_at` returned ~now; it must return the day-old edit.
    """
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (app / "__pycache__").mkdir()

    old = time.time() - 86400
    os.utime(app / "index.html", (old, old))
    os.utime(app, (old, old))

    updated = app_listing.dir_updated_at(str(app))
    assert updated == pytest.approx(old, abs=2), \
        "a bytecode cache is not an edit to the app"


def test_a_commit_does_not_make_an_app_look_recently_edited(tmp_path):
    """`.git` is excluded for the same reason and gets the same test: it is
    rewritten by every automatic commit, and the edit that triggered that commit
    has already moved a real file's mtime."""
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    (app / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")

    old = time.time() - 86400
    os.utime(app / "index.html", (old, old))
    os.utime(app, (old, old))

    assert app_listing.dir_updated_at(str(app)) == pytest.approx(old, abs=2)


def test_a_real_edit_still_registers(tmp_path):
    """The other half — the exclusions must not have made the whole signal inert.

    `dir_updated_at` exists because a dir's own mtime does not move when a file
    is edited in place, so an app edited now must still read as edited now even
    with both ignored children present and the dir itself stale.
    """
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    (app / "__pycache__").mkdir()
    (app / "index.html").write_text("<h1>edited just now</h1>", encoding="utf-8")
    os.utime(app, (time.time() - 86400,) * 2)

    assert app_listing.dir_updated_at(str(app)) == pytest.approx(time.time(), abs=5)
