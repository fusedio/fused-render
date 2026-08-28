"""Reader backing bundle/template.html: inspect a `.bundle` git bundle.

A bundle is a whole repository (or a slice of one) in a single file — what
`git bundle create` writes, and what the shell's folder Compress produces for a
repository. Opening one used to give nothing but a size and a Download link,
because it is binary and matches no other template.

The awkward fact this module exists to absorb: **a bundle is not a repository**.
`git bundle verify` flatly refuses to run outside one, and `git log` cannot be
pointed at a bundle at all. So every answer here is produced against a
throwaway repository created in temp for the duration of one call:

  * `verify`      — run from an EMPTY repo, which is precisely what makes the
                    prerequisite question meaningful. A thin bundle (created
                    from a revision RANGE) only carries the new commits and
                    depends on history the recipient must already have; an
                    empty repo has none, so git names exactly what is missing.
                    That is the recipient's-eye answer, and it is a NORMAL
                    state to render, not a failure.
  * `list-heads`  — the refs the bundle carries (this one works standalone).
  * history       — fetch the chosen ref out of the bundle into the throwaway
                    repo, then log it there.

The temp repo is always in the system temp dir, never beside the file, so a
bundle on read-only media still previews. Previewing writes nothing the user
can see; `clone` is the one action that puts something on disk, and only
because it was asked for.

Actions (``action`` param):
  - overview : refs, verify verdict, prerequisites, size, the clone command.
  - history  : commits reachable from `ref` (defaulting to the bundle's HEAD).
  - clone    : `git clone <file>` into a sibling directory named after it.

Refusals are payloads (``{"ok": False, "reason", "message"}``), never
exceptions: the page renders an empty state, never a traceback overlay.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile


# An ABSOLUTE git path is required to reach posix_spawn, not merely tidy: CPython
# forks unless `os.path.dirname(executable)` is truthy, and a fork in a process
# with libproj resident dies with SIGSEGV before exec (rc -11, no output, no
# exception). `close_fds=False` alone does NOT achieve this — see
# fused_render/server/gitignore.py and tests/test_git_posix_spawn.py.
_GIT_BIN = None


def _git_bin():
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op. (The
# markdown/graph.py and git/log.py pattern — without it the `../shared` hop in
# _is_mount_backed raises NameError the first time a clone is attempted.)
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "reader.py")

_HERE = os.path.dirname(os.path.abspath(__file__))

# Every git call is bounded. A bundle is a local file, so nothing here should
# take seconds — but a corrupt pack can make git spin, and an unbounded
# subprocess in a preview would park a request forever.
TIMEOUT_S = 30.0
# A clone unpacks the whole pack and writes a working tree, so it gets its own,
# much longer bound: this is a deliberate user action on a file whose size the
# page already showed them.
CLONE_TIMEOUT_S = 600.0

# One page of history, and the ceiling on a hand-edited `limit`.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# Non-interactivity, as environment: an archive viewer has no UI to answer a
# credential prompt, so anything that would ask must fail instead.
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

# Keep the output parseable whatever the user's config says (`-c` beats every
# config file, and only these knobs are touched).
_CONFIG = (
    "-c", "core.quotepath=false",
    "-c", "color.ui=false",
    "-c", "log.showSignature=false",
    "-c", "advice.detachedHead=false",
)

# `%x00`-delimited, one commit per line — every field is single-line by
# construction, so the newline is an unambiguous record separator.
_LOG_FORMAT = "%H%x00%h%x00%an%x00%aI%x00%ar%x00%s"
_LOG_FIELDS = ("sha", "short", "author", "date", "relative", "subject")


class _Refused(Exception):
    """A situation the view renders as an empty state. Carries its own payload."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.payload = {"ok": False, "reason": reason, "message": message}


# ------------------------------------------------------------------ invocation


def _popen_kwargs():
    """The spawn discipline, shared with every other git caller in this repo.

    `close_fds=False` is one of THREE things that together reach posix_spawn: an
    absolute argv[0] and the absence of a `cwd=` kwarg are the other two, and any
    one of them missing puts the call back on fork(), where a process with libproj
    resident dies in PROJ's atfork handler before exec — rc -11, no output, no
    exception. A plain dict literal on purpose: tests/test_git_posix_spawn.py
    reads it statically to verify the call sites that spread it.
    """
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _run(args, cwd, timeout=TIMEOUT_S, allow=(0,)):
    """One bounded git call. Returns (stdout_text, stderr_text, returncode).

    `cwd` becomes `-C <cwd>` in the argv rather than a `cwd=` kwarg (see
    _popen_kwargs). git chdirs there itself before the subcommand runs, so every
    relative path in `args` resolves exactly as it did before — and `dest` is
    already required to be absolute by _check_dest, so the clone is unaffected
    either way.

    `allow` is the set of exit codes that are ANSWERS rather than failures —
    `bundle verify` exits 1 for "this bundle needs commits you don't have",
    which is a verdict the page renders, not an error.
    """
    try:
        proc = subprocess.run(
            [_git_bin(), "--no-pager", *_CONFIG, "-C", cwd, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise _Refused("no-git", "git is not installed, or not on this app's PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise _Refused("timeout", f"git took longer than {timeout:.0f}s to answer.") from exc
    except OSError as exc:
        raise _Refused("no-git", f"git could not be started: {exc}") from exc
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode not in allow:
        raise _Refused("git-failed", _first_line(err) or f"git exited {proc.returncode}.")
    return out, err, proc.returncode


def _first_line(text):
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("error: "):
            cleaned = cleaned[len("error: "):]
        elif cleaned.startswith("fatal: "):
            cleaned = cleaned[len("fatal: "):]
        if cleaned:
            return cleaned
    return ""


# Marks every throwaway repo this module creates, so a destination can be
# recognised as sitting inside one (see _check_dest) even across calls.
_SCRATCH_PREFIX = "fused-render-bundle-"


def _clear_readonly_and_retry(func, path, _exc_info):
    """`shutil.rmtree`'s `onerror` hook: drop the read-only bit and retry once.

    git writes some of what it unpacks (loose objects, fetched packs — exactly
    what `verify`/history fill the scratch repo with) read-only, and POSIX and
    Windows disagree about what that means for DELETING them: POSIX consults
    the parent DIRECTORY's write permission, so a read-only file under a
    writable temp dir unlinks fine and `rmtree` never even calls this hook
    there. Windows enforces the file's own read-only attribute against the
    delete itself, so `os.remove`/`os.rmdir` raises PermissionError on it — and
    `ignore_errors=True` used to swallow exactly that, which meant this scratch
    dir simply stopped being removable on Windows and every call to this
    module leaked one `fused-render-bundle-*` directory into the temp folder,
    forever. `os.chmod` + a single retry is the standard fix; a second failure
    (something genuinely locked, e.g. a virus scanner mid-scan) is left alone —
    this is best-effort cleanup of a throwaway dir, never something a caller
    should see fail.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


class _Scratch:
    """A throwaway git repository, for the length of one call.

    In the system temp dir, NEVER beside the bundle: the bundle may sit on
    read-only media, or in a folder the user would rather we left alone. It is
    also always empty at creation, which is the whole trick behind the
    prerequisite report — see the module docstring."""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix=_SCRATCH_PREFIX)
        _run(["init", "-q"], cwd=self.root)
        return self.root

    def __exit__(self, *exc):
        # `onerror=` rather than `ignore_errors=True` — see
        # `_clear_readonly_and_retry`. `onerror` (not the 3.12+ `onexc`) is the
        # spelling that still works on every Python version this ships on.
        shutil.rmtree(self.root, onerror=_clear_readonly_and_retry)
        return False


# ------------------------------------------------------------------- reading


def _check_file(file):
    if not file or not isinstance(file, str):
        raise _Refused("missing", "No bundle file was given.")
    if not os.path.exists(file):
        raise _Refused("missing", f"{os.path.basename(file)} no longer exists.")
    if os.path.isdir(file):
        raise _Refused("missing", f"{os.path.basename(file)} is a folder, not a bundle file.")
    if os.path.getsize(file) == 0:
        raise _Refused("empty", "This file is empty — there is no bundle in it.")


# git's own words for "this is not one of my files". Matched (rather than just
# passed through) so the page can say so in its own voice and offer nothing but
# the download.
_NOT_A_BUNDLE = ("does not look like a v2 or v3 bundle",
                 "is not a bundle", "unrecognized header")


def _refs(file, scratch):
    """The refs the bundle carries. `list-heads` is the one bundle command that
    works without a repository, but it is still run from the scratch repo so no
    ambient repository's config can colour the answer."""
    out, err, _ = _run(["bundle", "list-heads", file], cwd=scratch, allow=(0, 1))
    lowered = err.lower()
    if any(marker in lowered for marker in _NOT_A_BUNDLE):
        raise _Refused("not-a-bundle",
                       f"{os.path.basename(file)} is not a git bundle file.")
    if err.strip() and not out.strip():
        raise _Refused("git-failed", _first_line(err))
    refs = []
    for line in out.splitlines():
        sha, _, name = line.partition(" ")
        name = name.strip()
        if not sha or not name:
            continue
        refs.append({
            "sha": sha,
            "short_sha": sha[:7],
            "name": name,
            "short_name": _short_ref(name),
            "kind": _ref_kind(name),
        })
    return refs


def _short_ref(name):
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _ref_kind(name):
    if name.startswith("refs/heads/"):
        return "branch"
    if name.startswith("refs/tags/"):
        return "tag"
    if name == "HEAD":
        return "head"
    return "other"


def _verify(file, scratch):
    """Verify against the EMPTY scratch repo: any prerequisite is missing by
    construction, so git lists exactly what a recipient would need.

    Returns (complete, prerequisites, message)."""
    out, err, code = _run(["bundle", "verify", file], cwd=scratch, allow=(0, 1))
    lowered = err.lower()
    if any(marker in lowered for marker in _NOT_A_BUNDLE):
        raise _Refused("not-a-bundle",
                       f"{os.path.basename(file)} is not a git bundle file.")
    if code == 0:
        # git's verdict line, not just its first line of output — the first is
        # "The bundle contains these N refs:", which is a header, not a verdict.
        verdict = next((ln.strip() for ln in out.splitlines()
                        if ln.strip().startswith("The bundle records")), "")
        return True, [], verdict or "This bundle records a complete history."
    prereqs = []
    for line in err.splitlines():
        text = line.strip()
        if text.startswith("error: "):
            text = text[len("error: "):]
        sha, _, note = text.partition(" ")
        if len(sha) >= 40 and all(c in "0123456789abcdef" for c in sha.lower()):
            prereqs.append({"sha": sha, "short_sha": sha[:7], "note": note.strip()})
    if not prereqs:
        # A rc=1 with nothing that looks like a prerequisite is a real failure
        # (a truncated or corrupt pack), not a thin bundle.
        raise _Refused("git-failed", _first_line(err) or "git could not verify this bundle.")
    return False, prereqs, ("This bundle is incomplete: it only makes sense in a "
                            "repository that already has the commits below.")


def _default_ref(refs):
    """The ref the page opens first: whatever HEAD points at if the bundle
    carries one, else main/master, else the first branch, else the first ref."""
    head = next((r for r in refs if r["name"] == "HEAD"), None)
    branches = [r for r in refs if r["kind"] == "branch"]
    if head is not None:
        same = next((r for r in branches if r["sha"] == head["sha"]), None)
        if same is not None:
            return same["name"]
    for preferred in ("refs/heads/main", "refs/heads/master"):
        if any(r["name"] == preferred for r in branches):
            return preferred
    if branches:
        return branches[0]["name"]
    return refs[0]["name"] if refs else ""


def _overview(file, scratch):
    refs = _refs(file, scratch)
    complete, prereqs, message = _verify(file, scratch)
    return {
        "ok": True,
        "file": file,
        "name": os.path.basename(file),
        "size": os.path.getsize(file),
        "refs": refs,
        "complete": complete,
        "prerequisites": prereqs,
        "message": message,
        "default_ref": _default_ref(refs),
        # How a bundle is actually consumed. Shown verbatim for copying.
        "clone_command": f"git clone {_quote(file)} {_quote(_free_dest(file))}",
        "clone_dest": _free_dest(file),
    }


def _history(file, scratch, ref, limit):
    refs = _refs(file, scratch)
    ref = ref or _default_ref(refs)
    # `ref` is client input and would otherwise become an argv entry, so it is
    # checked against the bundle's OWN ref list rather than pattern-matched:
    # nothing git did not just name can reach the command line.
    known = {r["name"] for r in refs} | {r["short_name"] for r in refs}
    if ref not in known:
        raise _Refused("unknown-ref", f"This bundle has no ref called {ref!r}.")
    full = next((r["name"] for r in refs
                 if ref in (r["name"], r["short_name"])), ref)
    limit = min(max(1, int(limit or DEFAULT_LIMIT)), MAX_LIMIT)

    # A bundle can be fetched from like a remote; `git log` cannot read one
    # directly. Pulling the ref into the scratch repo is what makes a log
    # possible at all — and it is also the point where a thin bundle's missing
    # prerequisites become fatal, which is a state to explain, not a stack.
    _, err, code = _run(["fetch", "--no-tags", "-q", file, f"{full}:refs/heads/preview"],
                        cwd=scratch, allow=(0, 1, 128))
    if code != 0:
        if "prerequisite" in err.lower() or "not our ref" in err.lower():
            raise _Refused("prerequisites",
                           "This bundle's history can't be read on its own — it needs "
                           "prerequisite commits that only the original repository has.")
        raise _Refused("git-failed", _first_line(err) or f"git exited {code}.")

    # limit + 1 so "there is more" is an observation rather than a guess.
    out, _, _ = _run(["log", f"--format={_LOG_FORMAT}", f"-n{limit + 1}",
                      "refs/heads/preview"], cwd=scratch)
    commits = []
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) == len(_LOG_FIELDS):
            commits.append(dict(zip(_LOG_FIELDS, parts)))
    has_more = len(commits) > limit
    return {
        "ok": True,
        "ref": full,
        "short_ref": _short_ref(full),
        "commits": commits[:limit],
        "has_more": has_more,
        "limit": limit,
        "max_limit": MAX_LIMIT,
    }


# --------------------------------------------------------------------- clone


def _quote(path):
    """Shell-quote for DISPLAY only — this string is shown for copying, never
    executed here (every real invocation is an argv list)."""
    return f'"{path}"' if any(c in path for c in ' \t"\'\\$`') else path


def _free_dest(file):
    """The sibling directory a clone would land in: project.bundle ->
    <dir>/project, then "project 2" and so on, matching how the shell's own
    Compress numbers a name it can't have."""
    real = os.path.realpath(file)
    parent = os.path.dirname(real)
    stem = os.path.basename(real)
    for suffix in (".bundle", ".bndl"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem or "bundle"
    candidate = os.path.join(parent, stem)
    counter = 1
    while os.path.exists(candidate) and counter < 100:
        counter += 1
        candidate = os.path.join(parent, f"{stem} {counter}")
    return candidate


def _is_mount_backed(path):
    """Whether `path` sits under the app's mounts dir, via shared/appenv — the
    one home for the mount rule, the same bridge git/condition.py uses (a
    template must not import fused_render, SPEC PY-15).

    Fails CLOSED: if appenv cannot be imported we cannot tell, and the safe
    answer for a write target is to refuse rather than to write."""
    shared = os.path.join(os.path.dirname(_HERE), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    try:
        from appenv import is_mount_backed
    except Exception as exc:  # noqa: BLE001 — cannot tell -> refuse
        raise _Refused("mount-unsupported",
                       "Can't tell whether that folder is on a mount, so the "
                       "clone was not attempted.") from exc
    return is_mount_backed(path)


def _within(root, target):
    """True when `target` is `root` or sits inside it, compared canonically so
    a symlink or a /var -> /private/var alias can't slip past."""
    root = os.path.normcase(os.path.realpath(root))
    target = os.path.normcase(os.path.realpath(target))
    return target == root or target.startswith(root + os.sep)


def _check_dest(dest, scratch):
    """Validate a caller-supplied clone destination.

    `dest` used to be entirely server-derived; it is now client input that
    decides where bytes land, so it gets the same treatment as any other write
    target. The rules are ordered cheapest-and-most-fundamental first, and the
    last two exist because the failure path DELETES `dest`: it must be
    impossible for `dest` to name anything this call did not create."""
    if not dest or not isinstance(dest, str) or not dest.strip():
        raise _Refused("bad-dest", "No destination folder was chosen.")
    dest = os.path.normpath(dest)
    if not os.path.isabs(dest):
        raise _Refused("bad-dest", "The destination must be an absolute path.")

    # Before any filesystem probe of the destination, like _fs_compress's mount
    # branch: a clone into the rclone VFS means writing a whole working tree
    # and object store through the cache, which is exactly the pattern that
    # wedges a mount.
    if _is_mount_backed(dest):
        raise _Refused("mount-unsupported",
                       "That folder is on a mounted location; cloning there "
                       "isn't supported.")

    # Nothing may land in a throwaway scratch tree — they are rmtree'd the
    # moment their call returns, so a clone placed inside one would be deleted
    # exactly when it succeeded. Scoped to OUR scratch dirs (this call's, and
    # any concurrent call's, by their shared prefix) rather than to the whole
    # system temp dir: `/tmp/somewhere` is a perfectly ordinary place to want a
    # clone, and pytest's own tmp_path lives under temp too.
    if _within(scratch, dest) or any(
            part.startswith(_SCRATCH_PREFIX) for part in dest.split(os.sep)):
        raise _Refused("bad-dest",
                       "That folder is inside a temporary working directory "
                       "that gets cleaned up; choose somewhere else.")

    parent = os.path.dirname(dest)
    if not os.path.isdir(parent):
        raise _Refused("missing-parent",
                       f"{parent} doesn't exist, so there is nowhere to clone into.")
    if os.path.exists(dest):
        raise _Refused("exists",
                       f"{os.path.basename(dest)} already exists — pick another "
                       "name or another folder.")
    # The same writability question the server asks of any new entry: a new
    # directory needs W_OK (and X_OK to traverse) on its PARENT.
    if not os.access(parent, os.W_OK | os.X_OK):
        raise _Refused("readonly",
                       f"{os.path.basename(parent) or parent} is read-only, so "
                       "the clone can't be written there.")
    return dest, parent


def _clone(file, scratch, dest):
    complete, prereqs, _ = _verify(file, scratch)
    if not complete:
        raise _Refused("prerequisites",
                       "This bundle can't be cloned on its own: it needs "
                       f"{len(prereqs)} commit(s) it doesn't carry. Fetch it into "
                       "a repository that already has them instead.")
    # No destination chosen (an older caller, or the page's own default): fall
    # back to the sibling _free_dest still names for the clone_command.
    dest, parent = _check_dest(dest or _free_dest(file), scratch)

    # _check_dest has just established that `dest` does not exist, so anything
    # standing there afterwards was created by THIS clone — which is the whole
    # licence for the rmtree below. Re-read it here rather than trusting the
    # check from a distance: the cleanup is destructive and the invariant it
    # rests on should be visible at the point it is used.
    preexisting = os.path.exists(dest)
    _, err, code = _run(["clone", "-q", file, dest], cwd=parent,
                        timeout=CLONE_TIMEOUT_S, allow=(0, 128))
    if code != 0:
        if not preexisting and os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        raise _Refused("git-failed", _first_line(err) or f"git clone exited {code}.")
    return {"ok": True, "dest": dest, "name": os.path.basename(dest)}


# ---------------------------------------------------------------------- entry


def main(file: str, action: str = "overview", ref: str = "", limit: int = 50,
         dest: str = "") -> dict:
    try:
        _check_file(file)
        if action not in ("overview", "history", "clone"):
            raise _Refused("unknown-action", f"Unknown action {action!r}.")
        with _Scratch() as scratch:
            if action == "overview":
                return _overview(file, scratch)
            if action == "history":
                return _history(file, scratch, ref, limit)
            return _clone(file, scratch, dest)
    except _Refused as refused:
        return refused.payload
    except OSError as exc:
        return {"ok": False, "reason": "unreadable",
                "message": f"Could not read this bundle: {exc}"}


# The fused-render engine / app runner only invoke a @fused.udf-registered
# entrypoint; a bare main() returns null under them. Register main via the shim
# (the house pattern — canvas/las/usd/git readers) so it runs under the engine,
# while `main` stays a plain callable for the built-in executor and for tests.
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
