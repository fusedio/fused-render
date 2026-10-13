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
from concurrent.futures import ThreadPoolExecutor

#: `<root>/<MANAGED_DIR>/<name>` — the CLI's own location for its worktrees.
MANAGED_DIR = os.path.join(".claude", "worktrees")
#: The line `create_worktree` adds to `.git/info/exclude`.
EXCLUDE_LINE = ".claude/worktrees/"

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Seconds. `git worktree add` checks out a whole tree, so it gets most of the
#: route's 20 s; the status read behind `dirty` gets 2 s per worktree (SPEC),
#: and an answer of "unknown" beats a menu that never opens.
QUICK_S = 5.0
ADD_S = 15.0
STATUS_S = 2.0

#: Same env and `-c` flags as `templates/git/ops.py`: never prompt for a
#: credential, never page, never colour, raw bytes in paths.
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_ASKPASS": "",
        "SSH_ASKPASS": "", "GCM_INTERACTIVE": "Never", "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_OPTIONAL_LOCKS": "0"}
_CONFIG = ("-c", "core.quotepath=false", "-c", "color.ui=false",
           "-c", "advice.detachedHead=false")


class _Refusal(Exception):
    """A one-line reason a public function answers `{"error": ...}` with."""


def _git(root: str, *args: str, timeout: float = QUICK_S) -> tuple[int, str, str]:
    """One bounded git call in `root`: `(returncode, stdout, stderr)`.

    argv[0] ABSOLUTE, `close_fds=False` and no `cwd=` (`-C root` instead): the
    three together are what let CPython use posix_spawn rather than fork, and a
    fork in this process (libproj resident) can SIGSEGV before exec and turn
    every git answer into a silent negative — `templates/git/ops.py` has the
    long version. Raises `subprocess.TimeoutExpired` (the child is killed), and
    a `_Refusal` when there is no git; the public functions catch both.
    """
    argv = [shutil.which("git") or "git", "--no-pager", *_CONFIG, "-C", root,
            *args]
    try:
        proc = subprocess.run(
                argv, capture_output=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env={**os.environ, **_ENV},
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


def _common_dir(start: str) -> str:
    """The shared `.git` directory for `start`'s repo (the MAIN one's, even
    from inside a linked worktree), absolute and resolved; "" when none."""
    rc, out, _ = _git(start, "rev-parse", "--git-common-dir")
    out = out.strip()
    if rc != 0 or not out:
        return ""
    return os.path.realpath(os.path.join(start, out))


def repo_root(path: str) -> str:
    """The MAIN repository root for any path inside a repo or one of its
    linked worktrees — the parent of `git rev-parse --git-common-dir`, which
    is the one answer a worktree and its main checkout share (`--show-toplevel`
    would name the worktree). "" when not a repo or git is missing; a bare
    repo has no checkout to put worktrees beside and also answers ""."""
    try:
        start = _start_dir(path)
        if not start:
            return ""
        common = _common_dir(start)
        if not common or os.path.basename(common) != ".git":
            return ""
        return os.path.dirname(common)
    except (_Refusal, OSError, subprocess.SubprocessError):
        return ""


def _root_or_refuse(path: str) -> str:
    root = repo_root(path)
    if not root:
        if not _start_dir(path):
            raise _Refusal(f"no such folder: {path!r}")
        if shutil.which("git") is None:
            raise _Refusal("git is not installed")
        raise _Refusal("not a git repository")
    return root


def _within(child: str, parent: str) -> bool:
    """Is `child` strictly inside `parent` (both resolved)?"""
    try:
        return (child != parent
                and os.path.commonpath([child, parent]) == parent)
    except ValueError:  # different drives on Windows
        return False


def _managed_dir(root: str) -> str:
    return os.path.join(root, MANAGED_DIR)


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


def _porcelain(root: str) -> list[dict]:
    """`git worktree list --porcelain` as `[{path, head, branch}]`, main
    first (git's order). Blank-line separated records; `branch` is the short
    name, "" when detached. A bare main entry is skipped."""
    rc, out, err = _git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        raise _Refusal(_said(err))
    rows, cur = [], None
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur and not cur.pop("bare", False):
                rows.append(cur)
            cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": os.path.realpath(val), "head": "", "branch": ""}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "bare":
            cur["bare"] = True
    return rows


@_guarded
def list_worktrees(path: str) -> dict:
    """Every worktree of `path`'s repo, and which one `path` is in.

    `{"root", "current", "branch_current", "worktrees": [{"path", "branch",
    "head", "name", "managed", "is_main", "dirty"}]}`. `current` is the
    worktree whose folder holds `path` (the deepest match, so a managed
    worktree nested under the main checkout wins over main), else `root`.
    The dirty reads run side by side — each is bounded by `STATUS_S`, and in
    series a dozen worktrees would eat the route's whole budget."""
    root = _root_or_refuse(path)
    start = os.path.realpath(_start_dir(path))
    managed_dir = _managed_dir(root)
    rows = _porcelain(root)
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
    return {"root": root, "current": current, "branch_current": branch_current,
            "worktrees": worktrees}


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        raise _Refusal("a worktree name is 1-64 letters, digits, '.', '_' or '-'")
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


def _free_branch(root: str, name: str) -> str:
    """`name`, else `name-2`, `name-3`… — the first not already a branch.
    One `for-each-ref` read, not a probe per candidate."""
    rc, out, err = _git(root, "for-each-ref", "--format=%(refname:short)",
                        "refs/heads/")
    if rc != 0:
        raise _Refusal(_said(err))
    taken = set(out.split())
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


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
    root = _root_or_refuse(path)
    start = _start_dir(path)
    target = os.path.join(_managed_dir(root), name)
    if os.path.lexists(target):
        raise _Refusal(f"{os.path.join(MANAGED_DIR, name)} already exists")
    if not isinstance(base, str):
        base = ""
    base = base.strip()
    if base.startswith("-"):
        raise _Refusal(f"not a valid base: {base!r}")
    rc, out, err = _git(start, "rev-parse", "--verify", "--quiet",
                        (base or "HEAD") + "^{commit}")
    if rc != 0 or not out.strip():
        raise _Refusal(f"no such commit: {base!r}" if base
                       else "this checkout has no commits yet")
    commit = out.strip()
    branch = _free_branch(root, name)
    rc, _, err = _git(root, "check-ref-format", "--branch", branch)
    if rc != 0:
        raise _Refusal(f"not a valid branch name: {branch!r}")
    _exclude(_common_dir(root))
    os.makedirs(_managed_dir(root), exist_ok=True)
    rc, _, err = _git(root, "worktree", "add", "-b", branch, target, commit,
                      timeout=ADD_S)
    if rc != 0:
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
    root = _root_or_refuse(path)
    managed_dir = _managed_dir(root)
    if isinstance(name, str) and os.path.isabs(name):
        target = os.path.realpath(name)
    else:
        target = os.path.join(managed_dir, _check_name(name))
    if os.path.dirname(target) != managed_dir:
        raise _Refusal("only worktrees under .claude/worktrees/ can be removed")
    if target not in {r["path"] for r in _porcelain(root)}:
        raise _Refusal(f"no worktree named {os.path.basename(target)!r}")
    if not force:
        d = _dirty(target)
        if d is None:
            raise _Refusal("could not tell whether this worktree has changes")
        if d:
            raise _Refusal("this worktree has uncommitted changes")
    args = ["worktree", "remove", *(["--force"] if force else []), target]
    rc, _, err = _git(root, *args, timeout=ADD_S)
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
