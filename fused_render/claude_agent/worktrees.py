"""Git worktrees for the chat: list, create, remove (SPEC W-2).

WHY FUSED-RENDER OWNS THE WORKTREE, not `claude --worktree`: a chat is keyed on
its working directory everywhere — the session-store munge, the Tasks page
rows, the project queue (`project_queue.queue_key`, where a `.git` FILE makes a
linked worktree its own key), resume by `_file`. A worktree made by `git
worktree add` and then opened as an ordinary DIRECTORY target gets all of that
for nothing; the CLI flag would move the cwd under the chat and make `--resume`
ambiguous. So the chat's `_file` simply becomes the worktree's path.

WHERE THEY LIVE is the CLI's own convention, `<repo-root>/.claude/worktrees/
<name>`, so the CLI's `EnterWorktree` tool and this module agree on what a
"managed" worktree is. That folder is listed in `<repo>/.git/info/exclude` on
first create — never in the tracked `.gitignore`, which is the user's file and
would show up as a change in their next commit — so the main checkout's status
does not report every worktree as untracked.

Stdlib and the `git` binary only (`templates/git/ops.py` is the project's git
plumbing, but `fused_render` never imports from `templates/`). NOTHING RAISES
PAST THIS MODULE: every failure is `{"error": "<one line>"}`, the shape the
chat's other actions answer with, so the route returns it as a 200 answer.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

#: `<root>/<MANAGED_DIR>/<name>` — the CLI's own location for its worktrees.
MANAGED_DIR = os.path.join(".claude", "worktrees")
#: The line `create_worktree` adds to `.git/info/exclude`.
EXCLUDE_LINE = ".claude/worktrees/"

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: Windows device names: a folder called `nul` or `com1` cannot be created (or
#: removed) there, whatever the extension, so they are refused everywhere —
#: a repo is shared between machines.
RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)])

#: Seconds. ONE DEADLINE PER CALL, not a timeout per git command: the route
#: gives the whole call 20 s (SPEC W-3), and a create is six git commands, so
#: per-command timeouts would add up past it and the route would 504 with git
#: still running. Every command gets `deadline - now` (capped by its own
#: limit); `BUDGET_S` leaves `CLEANUP_S` of the 20 s for undoing a failed add.
BUDGET_S = 16.0
QUICK_S = 5.0
STATUS_S = 2.0
#: Each of the (up to five) best-effort commands that undo a failed add.
CLEANUP_S = 0.6

#: Same env and `-c` flags as `templates/git/ops.py`: never prompt for a
#: credential, never page, never colour, raw bytes in paths.
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_ASKPASS": "",
        "SSH_ASKPASS": "", "GCM_INTERACTIVE": "Never", "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_OPTIONAL_LOCKS": "0"}
_CONFIG = ("-c", "core.quotepath=false", "-c", "color.ui=false",
           "-c", "advice.detachedHead=false")
#: Inherited variables that would point git at ANOTHER repository than the
#: `-C root` we name: a server launched from a git hook, or a developer shell
#: with `GIT_DIR` exported, would otherwise have every call here (the add, the
#: forced remove, the cleanup's `branch -D`) act on that repository instead.
#: Dropped from the child environment before `_ENV` is merged (CodeRabbit,
#: PR #1635).
_GIT_LOCATION_VARS = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
    "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES"})


def _child_env() -> dict:
    """The environment every git call here runs under: ours minus the
    repository-location variables, plus the non-interactive pins."""
    return {**{k: v for k, v in os.environ.items()
               if k not in _GIT_LOCATION_VARS}, **_ENV}


class _Refusal(Exception):
    """A one-line reason a public function answers `{"error": ...}` with."""


def _deadline() -> float:
    return time.monotonic() + BUDGET_S


def _git(root: str, *args: str, timeout: float = QUICK_S,
         deadline: float | None = None) -> tuple[int, str, str]:
    """One bounded git call in `root`: `(returncode, stdout, stderr)`.

    argv[0] ABSOLUTE, `close_fds=False` and no `cwd=` (`-C root` instead): the
    three together are what let CPython use posix_spawn rather than fork, and a
    fork in this process (libproj resident) can SIGSEGV before exec and turn
    every git answer into a silent negative — `templates/git/ops.py` has the
    long version. Raises `subprocess.TimeoutExpired` (the child is killed), and
    a `_Refusal` when there is no git or `deadline` (monotonic) has passed;
    the public functions catch both.

    On Windows, `subprocess.run` reads the pipes once more after killing a
    timed-out child, so a grandchild still holding them (a hook, a credential
    helper) can make that read block past `timeout`. POSIX only waits on the
    child, which the kill has ended.
    """
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0.05:
            raise _Refusal("git ran out of time")
    argv = [shutil.which("git") or "git", "--no-pager", *_CONFIG, "-C", root,
            *args]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=_child_env(),
            close_fds=False,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0))
    except FileNotFoundError:
        raise _Refusal("git is not installed") from None
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _said(err: str) -> str:
    """git's diagnosis as one line: the first `fatal:`/`error:` line (else
    the first line), `hint:` advice dropped — it is advice for a terminal."""
    lines = [ln.strip() for ln in err.splitlines()
             if ln.strip() and not ln.strip().lower().startswith("hint:")]
    diag = [ln for ln in lines if ln.lower().startswith(("fatal:", "error:"))]
    return (diag or lines or ["git failed"])[0]


def _guarded(fn):
    """The no-raise contract in one place: a `_Refusal` is its own message,
    no git or a timeout says so, anything else is its exception text."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _Refusal as exc:
            return {"error": str(exc)}
        except subprocess.TimeoutExpired as exc:
            return {"error": f"git took longer than {exc.timeout:g} s"}
        except Exception as exc:  # noqa: BLE001 — the module never raises
            return {"error": f"{type(exc).__name__}: {exc}".splitlines()[0]}
    return wrapper


def _start_dir(path: str) -> str:
    """The directory git is asked from: `agent._workdir`'s rule (a directory
    is itself, a file its parent), then up to the nearest folder that exists
    — a chat whose file was just deleted is still inside its repo."""
    if not isinstance(path, str) or not path.strip():
        return ""
    d = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(d):
        d = os.path.dirname(d)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    return d


def _records(root: str, deadline: float | None = None) -> list[dict]:
    """`git worktree list --porcelain` as `[{path, head, branch, bare}]`, main
    first (git's order). Blank-line separated records; `branch` is the short
    name, "" when detached.

    The MAIN record's `path` is not trustworthy: for a `--separate-git-dir`
    repo or a submodule, git (2.43) prints the git dir there, not the
    checkout. `_locate` works the main root out another way and the callers
    overwrite it; linked worktrees' paths are git's own record and are right.
    """
    rc, out, err = _git(root, "worktree", "list", "--porcelain",
                        deadline=deadline)
    if rc != 0:
        raise _Refusal(_said(err))
    rows, cur = [], None
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                rows.append(cur)
            cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": os.path.realpath(val), "head": "", "branch": "",
                   "bare": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "bare":
            cur["bare"] = True
    return rows


def _locate(path: str, deadline: float | None = None
            ) -> tuple[str, str, list[dict]]:
    """`(main root, common git dir, worktree records)` for `path`; `root` is ""
    when there is no checkout to hang worktrees off.

    The main root is NOT `dirname(--git-common-dir)`: that only holds when the
    git dir is a `.git` folder inside the checkout, and a `--separate-git-dir`
    repo or a submodule (git dir under the superproject's `.git/modules/`)
    would read as "not a repository". Instead, in order:

    * asked from the main checkout itself (git dir == common dir), it is
      `--show-toplevel`;
    * from a linked worktree, the common dir's `core.worktree` when set
      (submodules set it), else the porcelain's first record unless that
      merely names the git dir (the separate-git-dir case, where nothing
      records the main checkout's location and the answer is "").

    A bare repo (the first record says `bare`) answers "": no main checkout.
    """
    start = _start_dir(path)
    if not start:
        return "", "", []
    rc, out, _ = _git(start, "rev-parse", "--absolute-git-dir",
                      "--git-common-dir", deadline=deadline)
    lines = out.splitlines()
    if rc != 0 or len(lines) < 2:
        return "", "", []
    git_dir = os.path.realpath(lines[0])
    common = os.path.realpath(os.path.join(start, lines[1]))
    rows = _records(start, deadline)
    if not rows or rows[0]["bare"]:
        return "", common, rows
    root = ""
    if git_dir == common:
        rc, out, _ = _git(start, "rev-parse", "--show-toplevel",
                          deadline=deadline)
        if rc == 0 and out.strip():
            root = os.path.realpath(out.strip())
    else:
        rc, out, _ = _git(start, "config", "--file",
                          os.path.join(common, "config"), "--get",
                          "core.worktree", deadline=deadline)
        if rc == 0 and out.strip():
            root = os.path.realpath(os.path.join(common, out.strip()))
        elif rows[0]["path"] != common:
            root = rows[0]["path"]
    if root:
        rows[0]["path"] = root
    return root, common, rows


def repo_root(path: str) -> str:
    """The MAIN repository root for any path inside a repo or one of its
    linked worktrees (`--show-toplevel` would name the worktree; `_locate`
    has how). "" when not a repo, a bare repo, or git is missing."""
    try:
        return _locate(path, _deadline())[0]
    except (_Refusal, OSError, subprocess.SubprocessError):
        return ""


def _located_or_refuse(path: str, deadline: float
                       ) -> tuple[str, str, list[dict]]:
    root, common, rows = _locate(path, deadline)
    if not root:
        if not _start_dir(path):
            raise _Refusal(f"no such folder: {path!r}")
        if rows and rows[0]["bare"]:
            raise _Refusal("a bare repository has no checkout for worktrees")
        raise _Refusal("not a git repository")
    return root, common, rows


def _within(child: str, parent: str) -> bool:
    """Is `child` strictly inside `parent` (both resolved)?"""
    try:
        return (child != parent
                and os.path.commonpath([child, parent]) == parent)
    except ValueError:  # different drives on Windows
        return False


def _managed_dir(root: str) -> str:
    return os.path.join(root, MANAGED_DIR)


def _check_managed_dir(root: str) -> str:
    """`<root>/.claude/worktrees`, refused when `.claude` or `worktrees` is a
    symlink or the folder resolves outside `root`: a link there would send a
    checkout (or a `remove --force`) to wherever it points."""
    managed = _managed_dir(root)
    for d in (os.path.dirname(managed), managed):
        if os.path.islink(d):
            raise _Refusal(f"{os.path.relpath(d, root)} is a symlink; "
                           "refusing to put worktrees through it")
    if os.path.exists(managed) and not _within(os.path.realpath(managed), root):
        raise _Refusal(".claude/worktrees resolves outside the repository")
    return managed


def _dirty(path: str) -> bool | None:
    """Uncommitted changes (untracked files count: `git worktree remove`
    refuses those too). None when git did not answer in `STATUS_S` or at all
    — a pruned worktree whose folder is gone included."""
    if not os.path.isdir(path):
        return None
    try:
        rc, out, _ = _git(path, "status", "--porcelain", timeout=STATUS_S)
    except (_Refusal, OSError, subprocess.SubprocessError):
        return None
    return bool(out.strip()) if rc == 0 else None


@_guarded
def list_worktrees(path: str) -> dict:
    """Every worktree of `path`'s repo, and which one `path` is in.

    `{"root", "current", "branch_current", "worktrees": [{"path", "branch",
    "head", "name", "managed", "is_main", "dirty"}]}`. `current` is the
    worktree whose folder holds `path` (the deepest match, so a managed
    worktree nested under the main checkout wins over main), else `root`.
    The dirty reads run side by side — each is bounded by `STATUS_S`, and in
    series a dozen worktrees would eat the route's whole budget."""
    root, _, rows = _located_or_refuse(path, _deadline())
    start = os.path.realpath(_start_dir(path))
    managed_dir = _managed_dir(root)
    with ThreadPoolExecutor(max_workers=8) as ex:
        dirty = list(ex.map(_dirty, [r["path"] for r in rows]))
    worktrees = []
    for i, (r, d) in enumerate(zip(rows, dirty)):
        worktrees.append({
            "path": r["path"], "branch": r["branch"], "head": r["head"],
            "name": os.path.basename(r["path"].rstrip(os.sep)) or r["path"],
            "managed": _within(r["path"], managed_dir),
            "is_main": i == 0 and r["path"] == root,
            "dirty": d,
        })
    holding = [w for w in worktrees
               if w["path"] == start or _within(start, w["path"])]
    current = max(holding, key=lambda w: len(w["path"]))["path"] if holding else root
    branch_current = next((w["branch"] for w in worktrees
                           if w["path"] == current), "")
    # THE SAME TARGET IN EVERY OTHER WORKTREE. A chat open on a FILE (or a
    # subfolder) of one checkout switches to that file in the other checkout
    # when it exists there, and to the checkout's root when it does not (a
    # file added on one branch only), so a switch never lands on a path that
    # is not on disk. `relative` is the path under `current`; the current
    # worktree maps to the target itself.
    target = os.path.realpath(path)
    relative = ""
    if target != current and _within(target, current):
        relative = os.path.relpath(target, current)
    for w in worktrees:
        if w["path"] == current:
            w["mapped"] = {"path": target, "exists": os.path.exists(target)}
            continue
        candidate = os.path.join(w["path"], relative) if relative else w["path"]
        w["mapped"] = {"path": candidate,
                       "exists": os.path.lexists(candidate)}
    return {"root": root, "current": current, "branch_current": branch_current,
            "relative": relative, "worktrees": worktrees}


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        raise _Refusal("a worktree name is 1-64 letters, digits, '.', '_' or '-'")
    if name.split(".")[0].lower() in RESERVED_NAMES:
        raise _Refusal(f"{name!r} is a reserved device name on Windows")
    return name


def _exclude(common: str) -> None:
    """`.claude/worktrees/` into `<common>/info/exclude`, once. The local,
    untracked ignore file: the managed folder sits inside the main checkout,
    and without this every worktree is an untracked change there."""
    path = os.path.join(common, "info", "exclude")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if any(ln.strip() in (EXCLUDE_LINE, "/" + EXCLUDE_LINE)
           for ln in text.splitlines()):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\n") else "\n")
                + EXCLUDE_LINE + "\n")


def _free_branch(root: str, name: str, deadline: float) -> str:
    """`name`, else `name-2`, `name-3`… — the first not already a branch.
    One `for-each-ref` read, not a probe per candidate.

    `lstrip=2`, not `:short`: `:short` prints `heads/x` for a branch `x`
    that a tag `x` shadows, and the collision would go unseen. Compared
    case-folded where the filesystem is (macOS, Windows): loose refs are
    files there, so `Feat` and `feat` are one ref."""
    rc, out, err = _git(root, "for-each-ref", "--format=%(refname:lstrip=2)",
                        "refs/heads/", deadline=deadline)
    if rc != 0:
        raise _Refusal(_said(err))
    fold = (str.casefold if sys.platform in ("darwin", "win32")
            else (lambda s: s))
    taken = {fold(b) for b in out.split()}
    candidate, n = name, 1
    while fold(candidate) in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def _undo_add(root: str, target: str, branch: str) -> None:
    """Best effort, after a `worktree add` that failed or was killed: git may
    have registered the worktree (locked "initializing" when it died mid
    checkout, which a plain or single-`--force` remove refuses), checked out
    part of it, and made the branch. Unlock, remove with `--force --force`,
    delete what is left of the folder (it did not exist before this call —
    `create_worktree` checked), prune, and drop the branch (free before this
    call, so whatever is there now is ours). Bounded by `CLEANUP_S` each, and
    no step's failure stops the next."""
    steps = (("worktree", "unlock", target),
             ("worktree", "remove", "--force", "--force", target))
    for args in steps:
        try:
            _git(root, *args, timeout=CLEANUP_S)
        except (_Refusal, OSError, subprocess.SubprocessError):
            pass
    if os.path.lexists(target):
        shutil.rmtree(target, ignore_errors=True)
    for args in (("worktree", "prune"), ("branch", "-D", "--", branch)):
        try:
            _git(root, *args, timeout=CLEANUP_S)
        except (_Refusal, OSError, subprocess.SubprocessError):
            pass


@_guarded
def create_worktree(path: str, name: str, base: str = "") -> dict:
    """`git worktree add -b <branch> <root>/.claude/worktrees/<name> <base>`
    → `{"path", "branch"}`.

    `base` defaults to the HEAD of the checkout `path` is in — a worktree cut
    from inside another worktree starts where that one is, not at main. The
    branch is `name` unless taken (then `name-2`…), because the folder name
    is what the user picked and a stale branch of the same name must not
    block it. An existing target folder is a refusal: git would refuse too,
    less legibly, and the folder may be someone's unrelated work."""
    _check_name(name)
    deadline = _deadline()
    root, common, _ = _located_or_refuse(path, deadline)
    start = _start_dir(path)
    managed = _check_managed_dir(root)
    target = os.path.join(managed, name)
    if os.path.lexists(target):
        raise _Refusal(f"{os.path.join(MANAGED_DIR, name)} already exists")
    if not isinstance(base, str):
        base = ""
    base = base.strip()
    if base.startswith("-"):
        raise _Refusal(f"not a valid base: {base!r}")
    rc, out, err = _git(start, "rev-parse", "--verify", "--quiet",
                        (base or "HEAD") + "^{commit}", deadline=deadline)
    if rc != 0 or not out.strip():
        raise _Refusal(f"no such commit: {base!r}" if base
                       else "this checkout has no commits yet")
    commit = out.strip()
    branch = _free_branch(root, name, deadline)
    rc, _, err = _git(root, "check-ref-format", "--branch", branch,
                      deadline=deadline)
    if rc != 0:
        raise _Refusal(f"not a valid branch name: {branch!r}")
    _exclude(common)
    os.makedirs(managed, exist_ok=True)
    _check_managed_dir(root)  # again: the folder may only now exist
    try:
        rc, _, err = _git(root, "worktree", "add", "-b", branch, target,
                          commit, timeout=BUDGET_S, deadline=deadline)
    except subprocess.TimeoutExpired as exc:
        _undo_add(root, target, branch)
        raise _Refusal(f"git worktree add took longer than {exc.timeout:.0f} s"
                       " and was undone") from None
    if rc != 0:
        _undo_add(root, target, branch)
        raise _Refusal(_said(err))
    return {"path": target, "branch": branch}


@_guarded
def remove_worktree(path: str, name: str, force: bool = False) -> dict:
    """`git worktree remove [--force]` on a MANAGED worktree → `{"removed":
    <path>}`. The branch is left alone — it may hold the only copy of
    committed work.

    `name` is a managed worktree's name or its absolute path (the menu has
    both). Anything that is not under `.claude/worktrees/` — the main
    checkout, a worktree the user made by hand — is refused: this app did not
    make it and has no business deleting it. A dirty worktree (or one whose
    state could not be read) is refused unless `force`, in words the confirm
    step can show."""
    deadline = _deadline()
    root, _, rows = _located_or_refuse(path, deadline)
    managed_dir = _check_managed_dir(root)
    if isinstance(name, str) and os.path.isabs(name):
        target = os.path.realpath(name)
    else:
        target = os.path.join(managed_dir, _check_name(name))
    if os.path.dirname(target) != managed_dir:
        raise _Refusal("only worktrees under .claude/worktrees/ can be removed")
    if target not in {r["path"] for r in rows[1:]}:
        raise _Refusal(f"no worktree named {os.path.basename(target)!r}")
    if not force:
        d = _dirty(target)
        if d is None:
            raise _Refusal("could not tell whether this worktree has changes")
        if d:
            raise _Refusal("this worktree has uncommitted changes")
    # `--force` TWICE: once for changes, once more for a LOCKED worktree (a
    # killed add leaves one locked "initializing"). The user already
    # confirmed discarding; a lock this app's own add left is no reason to
    # refuse them.
    args = ["worktree", "remove", *(["--force", "--force"] if force else []),
            target]
    rc, _, err = _git(root, *args, timeout=BUDGET_S, deadline=deadline)
    if rc != 0:
        raise _Refusal(_said(err))
    return {"removed": target}


def main(action: str = "list", file: str = "", name: str = "",
         base: str = "", force: bool = False) -> dict:
    """The route's one entry point (bound by `_binding.bind_params`, so
    `force` arrives as a bool from "1"/"true" as well as from JSON)."""
    if action == "list":
        return list_worktrees(file)
    if action == "create":
        return create_worktree(file, name, base)
    if action == "remove":
        return remove_worktree(file, name, force)
    return {"error": f"unknown worktree action: {action!r}"}
