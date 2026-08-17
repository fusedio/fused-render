"""Mutations backing `git/template.html` — the write half of the SCM view
(SPEC §33 / GT-12..GT-17).

`log.py` beside this file answers questions. This one changes things, and that
single difference is why it is a separate module rather than another `op` on the
reader: a reader that also mutates has no honest place to draw its validation
line, and a reviewer auditing "what can this template DO to my repository" has to
read one file rather than grep an 800-line reader for the verbs that write.

Everything is `git` shelled out to. Nothing here reimplements a git concept: not
what "staged" means, not what a valid branch name is, not what a fast-forward is.
The module's job is to build ONE bounded, un-shell-able argv per user gesture and
turn git's answer into JSON.

The ops, grouped by what they can cost you:

  safe        stage, unstage, stage_all, unstage_all, commit, branch_create,
              branch_checkout, stash_push, stash_apply, stash_pop, fetch, pull,
              push
  destructive discard, discard_all, stash_drop   ← can lose uncommitted work

Every destructive op is **individually addressable and never a side effect of a
safe one** (GT-16). The view confirms before calling them; that is the view's
job, and this module's job is to make sure the confirmation has exactly one thing
to be about.

What this module will NOT do, ever (GT-15):

* **Rewrite history.** No `--amend`, no hard resets, no rebase, no force push,
  no `branch -D`. Those are decisions with no undo, and git's own error message
  (or a terminal) is a better answer than a button.
* **`git commit -- <paths>`.** Commit is index-based, because that is what the
  word means in git. Committing a path list bypasses the index and records the
  *working tree* for those paths — so a file you deliberately staged in one state
  would be committed in another. Instead the reader reports `staged_outside` and
  the view warns that a commit will also carry it (GT-14).
* **Touch an ignored file.** `git clean` is run without `-x`, always. An ignored
  path is where a `.env`, a virtualenv and a build tree live, and "discard my
  edit" must never be able to mean "delete those".
* **Reach a mount-backed repository** (GT-4 / MD-11). The gate keeps the mode
  from being offered there; this module refuses regardless, because a
  hand-written `?_mode=git` URL bypasses the switcher entirely.

The security rules, each one a way this could go wrong:

* **argv lists only, never a shell string**, `--` before every pathspec, and
  every pathspec wrapped in `:(literal)` so a filename holding `*`, `?`, `[` or a
  leading `:` matches itself rather than becoming a glob or pathspec magic.
* **Every user-supplied path is validated before it becomes an argv entry**, in
  three passes that catch three different things: the STRING must not be
  absolute, hold a `..` segment or start with `-`; its REALPATH must resolve
  under the repository root (which is what catches a symlink, including an
  ordinary file reached through a symlinked parent); and it must additionally sit
  under the OPEN SCOPE, because a scoped view may only change what it shows.
* **A branch name is validated by git**, `git check-ref-format --branch`, not by
  a hand-rolled regex — the rules (no `..`, no `~^:?*[`, no trailing `.lock`, no
  `@{`, no control characters, no trailing slash) are git's and change with git.
  The one rule that CANNOT be delegated is a leading `-`: `check-ref-format` has
  no `--` terminator, so a dash-leading name would be read as its own option.
  That check therefore runs first, in Python.
* **A name GIT produced gets the same leading-dash rule** (`_repo_name`), because
  git echoes refnames and remote names verbatim out of files inside `.git`, and
  those files are content: a hand-written `.git/HEAD` makes `symbolic-ref` print
  `-evil`, and a hand-written `[remote "--receive-pack=<cmd>"]` makes `git remote`
  print that. Both then flow into argv. `--` before every such value is the other
  half of the same guarantee, and both halves are kept — depending on either
  alone is how one missing terminator becomes local command execution.
* **A stash index is a non-negative int** and is formatted into `stash@{n}` by
  us, so nothing user-shaped ever reaches a revision position — and it is
  additionally paired with the entry's **commit id**, because an index is a
  POSITION and every `stash push` renumbers every entry (see `_stash_at`).
* **A commit message is one argv element** to `-m` and may contain anything —
  newlines, quotes, `$(...)`, backticks. There is no shell for any of it to mean
  something to.

Refusal is a PAYLOAD, never an exception: `{"ok": false, "reason", "message"}`,
rendered in-view next to the control that caused it. Success is
`{"ok": true, "op", "detail"}` plus whatever the op has to say.

This module is exec'd standalone and **must not import `fused_render`** (SPEC
PY-15) — nor, for the same class of reason, `log.py`: sibling imports depend on
whatever `sys.path` the host happened to build, and `log` is a name almost
anything could shadow. So the shared primitives below are DUPLICATED from
`log.py` on purpose, each carrying a pointer to its twin. Keep the two in step.
"""
import os
import re
import subprocess
import sys

# Under the fused local execution backend a script is exec'd with its own
# directory first on sys.path but no __file__; rebuild it from there so the
# `../shared` hop below works in both hosts (log.py's twin of this block).
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "ops.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
# `../shared/appenv.py` is how a template asks the app about its environment
# (SPEC PY-15): env vars only, stdlib only, no `fused_render` import. The import
# stays LAZY at its use site so an unreachable appenv is still the "cannot tell"
# case that must read as a refusal.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))


# ------------------------------------------------------------------- the bounds

# Wall clock for one git invocation. Deliberately LONGER than the reader's 12s
# and still inside the 30s `fused.runPython` ceiling: a mutating command can run
# the user's own hooks (a `pre-commit` that lints, a `post-checkout` that
# rebuilds) and a network command talks to a remote, neither of which is
# comparable to a local plumbing read. Past this the process is killed and the
# view says so — which is a worse outcome for a WRITE than for a read, hence the
# extra headroom rather than the same number.
TIMEOUT_S = 25.0

# How many explicit paths one call may carry. The UI never sends more than a
# screenful, so this only ever catches a hand-written call — but an unbounded
# path list is an unbounded argv, and the failure mode of that is E2BIG from
# execve rather than anything a user could read.
MAX_PATHS = 500

# Bytes of git's stderr we are willing to quote back into a refusal. git's
# messages are short; a runaway hook's are not, and the message ends up in the
# DOM.
MAX_STDERR_BYTES = 4000

# Non-interactivity, as environment (log.py's twin, minus one entry).
#
# `GIT_TERMINAL_PROMPT=0` is what makes pull/push FAIL FAST instead of hanging:
# with a credential helper or an ssh-agent configured they work, and without one
# git would otherwise sit at a username prompt that nobody in an iframe can ever
# answer. The other askpass knobs close the same door from the GUI side.
#
# `GIT_OPTIONAL_LOCKS=0` is deliberately ABSENT, unlike in the reader. It only
# ever suppresses locks git takes OPTIONALLY — the opportunistic index refresh a
# read does while answering — and a mutating command takes the index lock it
# needs regardless. Carrying it here would state a promise this module cannot
# keep ("we do not lock"), and would suppress exactly the index refresh that
# makes the `git status` right after a mutation accurate.
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",
    "GIT_LFS_SKIP_SMUDGE": "1",
    # An editor would be launched by `commit` without `-m`, by a conflicted
    # `stash pop`, and by anything git decides needs a message. There is no
    # terminal here, so the editor is one that exits non-zero immediately and
    # turns "waiting forever" into an ordinary error.
    "GIT_EDITOR": "false",
    "GIT_SEQUENCE_EDITOR": "false",
}

# Config overrides that keep the output parseable regardless of the user's own
# config (log.py's twin, minus the diff-only knobs, which nothing here produces).
_CONFIG = (
    "-c", "core.quotepath=false",   # raw bytes in paths, not \NNN escapes
    "-c", "color.ui=false",         # never ANSI, even with color.ui=always
    "-c", "advice.detachedHead=false",
)

# `log.py`'s twin. Every field is single-line, so the newline separates records.
_COMMIT_FORMAT = "%h%x00%s"

# A hex object name, full or abbreviated (log.py's twin). Here it validates the
# stash id a destructive call quotes back — see `_stash_at` — so an id that is
# not an object name never reaches a revision position.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# The ops, split by what they can cost. The split is not cosmetic: the view keys
# its confirmation step off `DESTRUCTIVE`, and a new op that can lose work must
# be added here or it silently ships without a confirmation.
_SAFE_OPS = (
    "stage", "unstage", "stage_all", "unstage_all", "commit",
    "branch_create", "branch_checkout", "branch_delete",
    "stash_push", "stash_apply", "stash_pop",
    "fetch", "pull", "push",
)
#
# `resolve` is here rather than in `_SAFE_OPS` because it OVERWRITES a file in
# the working tree: the marked-up conflicted text is the only copy of "what git
# left me", and once it is replaced the only way back is `git checkout --merge`.
# So it gets the confirmation step every other work-losing op gets, and the
# proposed-resolution panel in front of it is a review surface, not the consent.
DESTRUCTIVE_OPS = ("discard", "discard_all", "stash_drop", "resolve")
_OPS = _SAFE_OPS + DESTRUCTIVE_OPS

# Ops that take an explicit `paths` list, and ops that operate on the whole open
# scope instead. Everything else takes neither.
_PATH_OPS = ("stage", "unstage", "discard", "resolve")
_SCOPE_OPS = ("stage_all", "unstage_all", "discard_all")

# git's own words for the two situations we have to recognise in its output
# rather than in an exit code, because it reports both as SUCCESS or as a generic
# failure. Matched case-insensitively against stderr+stdout.
_NOTHING_TO_STASH = "no local changes to save"
_NOT_FF = "not possible to fast-forward"

# The lines git writes into a conflicted file. Mirrors log.py's CONFLICT_MARKERS
# (the two modules are exec'd standalone, so neither may import the other) and is
# used for the opposite purpose: there to recognise a conflict, here to REFUSE
# content that still contains one.
CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

# A resolved file is one file's text. The bound is generous — it has to hold
# whatever the conflicted file held — and exists so a hand-written request cannot
# ask this module to write an arbitrary amount of data.
MAX_CONTENT_BYTES = 2_000_000


class _Refused(Exception):
    """A situation the view renders in place. Carries its own payload."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.payload = {"ok": False, "reason": reason, "message": message}


# ------------------------------------------------------------------ invocation


def _argv(root, args):
    """log.py's twin. `-C <root>` on everything, so a relative pathspec means
    exactly one thing and no `cd` can change it."""
    return ["git", "--no-pager", *_CONFIG, "-C", root, *args]


def _popen_kwargs():
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
        # close_fds=False matches every other subprocess spawn in this codebase
        # (app_git.py documents the crash at length): with libproj resident, a
        # plain fork() runs PROJ's pthread_atfork child handler into a SIGSEGV
        # before exec, and close_fds=False is what makes CPython take the
        # posix_spawn path that runs no atfork handlers. It matters more here
        # than in the reader: a read that dies before exec is a refused read,
        # while a `git commit` that dies before exec is work the user believes
        # they recorded.
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _clean(raw):
    """Everything git said on stderr, minus `hint:` lines. One line per line.

    `hint:` lines are dropped: they are advice for a terminal ("use 'git branch
    -D'…") and repeating them in a GUI that deliberately does not offer that
    verb would be telling the user to do something this view refuses to do.

    Deliberately NOT trimmed to a sentence here. Two situations are recognised by
    matching git's words (`_said`), so the text those matches run against must be
    whole; `_brief` does the trimming, at the point a message is built.
    """
    text = raw[:MAX_STDERR_BYTES].decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(line for line in lines
                     if not line.lower().startswith("hint:"))


def _brief(text):
    """The lines of `_clean`'s output a refusal should actually show.

    git puts its DIAGNOSIS on a `fatal:` / `error:` / `warning:` line and its
    progress chatter ("From /srv/repo", "* branch main -> FETCH_HEAD",
    "a1b2c3d..e4f5g6h main -> origin/main") on the lines around it — and how many
    of those there are depends on whether the fetch had anything to do, so their
    position varies between two runs of the same command.

    Picking the diagnosis rather than the first N lines is what keeps the
    sentence the user sees the one that explains the failure. The version that
    took the first three lines put `fatal: Not possible to fast-forward` fourth
    on a first-attempt pull, so the refusal arrived wearing the generic
    "git-failed" instead of the specific "your branches have diverged, do it in a
    terminal" — the message that was the whole point of refusing.
    """
    lines = [line for line in text.split("\n") if line]
    diagnostic = [line for line in lines
                  if line.lower().startswith(("fatal:", "error:", "warning:"))]
    return " ".join((diagnostic or lines)[:3])


def _run(root, *args):
    """One bounded git call; returns `(returncode, stdout_bytes, stderr_text)`.

    Unlike the reader's `_git`, this hands the exit code BACK rather than
    refusing on it, because a mutating op usually has something specific to say
    about one particular failure — a non-fast-forward pull, a branch that is not
    fully merged, a git too old to know `switch`. Callers with nothing special to
    say use `_git_ok` instead, which refuses in git's own words.

    Only the ways git can fail to RUN are refusals here: a missing binary, a
    timeout (which for a mutation may be the user's own pre-commit hook), or an
    OS-level spawn failure. None of those has an exit code to interpret.
    """
    try:
        proc = subprocess.run(
            _argv(root, args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_S,
            **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise _Refused("no-git", "git is not installed, or not on this app's PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise _Refused(
            "timeout",
            f"git took longer than {TIMEOUT_S:.0f}s and was stopped. If this "
            "repository has slow hooks, run the command in a terminal.") from exc
    except OSError as exc:
        raise _Refused("no-git", f"git could not be started: {exc}") from exc
    return proc.returncode, proc.stdout, _clean(proc.stderr)


def _said(out, err):
    """Everything git said, lowercased, across BOTH streams.

    Two situations here are recognisable only from git's WORDS — "No local
    changes to save" (which it reports with exit 0) and "Not possible to
    fast-forward". Which stream each lands on is not part of git's interface, and
    a guard watching only one would vanish silently if it ever moved. The failure
    mode that would produce is a button reporting success and doing nothing, so
    both streams are read.
    """
    return (out.decode("utf-8", "replace") + " " + err).lower()


def _git_ok(root, *args, allow=(0,)):
    """`_run`, but any exit code outside `allow` is a refusal in git's own words.

    Verbatim on purpose: `git branch -d` explaining that a branch is not fully
    merged, or `git switch` explaining that local changes would be overwritten,
    is better writing than anything this module could substitute — and it is the
    same sentence the user's terminal would give them.
    """
    code, out, err = _run(root, *args)
    if code not in allow:
        raise _Refused("git-failed", _brief(err) or f"git exited {code}.")
    return out


# ----------------------------------------------------------------- the location
#
# `_refuse_mounts`, `_locate` and `_pathspec` are log.py's twins. Kept duplicated
# rather than imported (see the module docstring): a template is exec'd
# standalone, and a sibling import depends on a sys.path the host builds.


def _refuse_mounts(path):
    """Refuse a mount-backed target outright (GT-4 / MD-11), write path included.

    The detector is `shared/appenv.is_mount_backed`, the app's own rule answered
    from `FUSED_RENDER_*` rather than by importing fused_render. An ImportError
    means we cannot tell, and "cannot tell" reads as "refuse" — which matters
    more here than in the reader: the thing being avoided is not a slow listing
    but a `git add` / `git commit` running across an rclone-NFS mount.
    """
    try:
        from appenv import is_mount_backed
    except Exception as exc:  # noqa: BLE001 — cannot tell -> refuse
        raise _Refused("mount", f"Mount detection unavailable ({exc}); "
                                "refusing to run git here.") from exc
    if is_mount_backed(path):
        raise _Refused(
            "mount",
            "Git operations are not available on remote mounts — git would have "
            "to walk the mounted tree, and a write would do it holding a lock.")


def _locate(file):
    """Resolve `(root, rel, is_dir)` for the open path, or refuse.

    `rel` is POSIX and relative to the work-tree root — "" for the root itself.
    It is both the scope pathspec and the containment boundary every path
    argument is checked against, so it is normalized once, here.
    """
    if not file:
        raise _Refused("missing", "No file or folder was given.")
    _refuse_mounts(file)
    path = os.path.abspath(file)
    if not os.path.exists(path):
        raise _Refused("missing", f"{path} does not exist.")
    is_dir = os.path.isdir(path)
    cwd = path if is_dir else os.path.dirname(path)

    # `-C cwd` cannot be used before the root is known, so this one call is the
    # bootstrap: the only invocation pinned to the target rather than the root.
    # `--show-toplevel` is empty for a bare repo and inside `.git`, which is the
    # same "no work tree to change" refusal as a non-repo.
    _, top, _ = _run(cwd, "rev-parse", "--show-toplevel")
    root = top.decode("utf-8", "replace").strip()
    if not root:
        raise _Refused("not-a-repo", f"{path} is not inside a git repository.")
    root = os.path.realpath(root)

    # realpath both sides: a repo reached through a symlink (or /var -> /private/var
    # on macOS) otherwise fails relpath's containment check for no real reason.
    real = os.path.realpath(path)
    rel = "" if real == root else os.path.relpath(real, root).replace(os.sep, "/")
    if rel.startswith("../"):
        raise _Refused("not-a-repo", f"{path} is not inside {root}.")
    return root, rel, is_dir


def _pathspec(rel):
    """The `--`-guarded pathspec list for a scope, or `[]` for the whole repo.

    log.py's twin. `:(literal)` disables pathspec magic and globbing, so a
    filename holding `*`, `?`, `[` — or a leading `:`, which would otherwise BE
    magic — matches itself. Directory-prefix matching still applies, which is
    what scoping to a folder needs.
    """
    return ["--", ":(literal)" + rel] if rel else ["--"]


def _spec(paths):
    """`--` plus one `:(literal)` pathspec per validated path."""
    return ["--", *(":(literal)" + p for p in paths)]


def _scope_spec(rel):
    """The pathspec meaning "everything in the open scope".

    NOT `_pathspec`, and the difference is a silent no-op rather than an error.
    At the repository root `_pathspec("")` is `["--"]`, i.e. `--` with no
    pathspec after it — which for a READ means "the whole repository" (git log
    with no paths logs everything) but for a WRITE means the empty path list, so
    `git add --` stages nothing and reports success. `:/` is git's own magic for
    "the top of the work tree", which is what the scope actually is there, and
    unlike `.` it does not depend on the process's cwd.
    """
    return ["--", ":(literal)" + rel] if rel else ["--", ":/"]


# ----------------------------------------------------------------- validation


def _check_resolve_content(paths, content):
    """Everything about a `resolve` that is decidable from the strings alone.

    All three refusals are this module's own rather than git's, because git is
    never asked: `resolve` writes a file and stages nothing, so there is no git
    command whose error could stand in for any of them.

    * **one path.** A resolution is one file's text; a `paths` list of two with
      one `content` cannot mean anything, and picking the first would write the
      wrong file.
    * **not empty.** Truncating a conflicted file to nothing is never a
      resolution, and it is exactly what an AI answer that arrived empty (a
      cancelled stream, a refusal, a fenced reply that cleaned to "") would do.
    * **no markers left.** Content that still carries `<<<<<<<` has not resolved
      the conflict, it has copied it — and writing it back makes the file look
      resolved-and-saved to everything downstream while leaving the markers in the
      source. This is the check that makes the feature safe to point at a model.
    """
    if len(paths) != 1:
        raise _Refused(
            "one-path",
            f"A resolution applies to ONE file; {len(paths)} were given.")
    if not isinstance(content, str) or not content.strip():
        raise _Refused("empty-content",
                       "There is no resolved content to write.")
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        raise _Refused(
            "too-large",
            f"That resolution is larger than {MAX_CONTENT_BYTES} bytes.")
    if any(any(line.startswith(m) or line == m.strip() for m in CONFLICT_MARKERS)
           for line in content.splitlines()):
        raise _Refused(
            "unresolved",
            "That text still contains conflict markers, so it is not a "
            "resolution. Nothing was written.")


def _resolve(root, rel, content):
    """Write the resolved text of ONE conflicted file, and do nothing else.

    Deliberately NOT followed by `git add`. Marking a conflict resolved is the
    act that lets the merge be committed, and it is not what the user pressed:
    they pressed "apply this proposal so I can look at it". So the file lands in
    the working tree, still unmerged as far as the index is concerned, and the
    ordinary Stage button — which the view already has, and which git's own
    `add` semantics make the resolve — is how it becomes resolved. No commit
    either, for the same reason and more so.

    The path must currently be UNMERGED. That is not a courtesy check: without it
    this op is a general-purpose "overwrite any file in the repo" write, which is
    not something this module offers. `--diff-filter=U` is git's own answer to
    "is this file conflicted", so the question is not re-implemented here.

    Written through a temporary file in the same directory and `os.replace`d, so
    an interrupted write cannot leave the conflicted file half-overwritten — the
    one outcome worse than not writing at all.
    """
    out = _git_ok(root, "diff", "--name-only", "--diff-filter=U", "-z",
                  *_spec([rel]), allow=(0, 1))
    unmerged = [c for c in out.decode("utf-8", "replace").split("\0") if c]
    if rel not in unmerged:
        raise _Refused(
            "not-conflicted",
            f"{rel} is not a conflicted file, so there is nothing to resolve "
            "in it. Nothing was written.")

    full = os.path.join(root, *rel.split("/"))
    tmp = full + ".fused-resolve.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp, full)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise _Refused("write-failed", f"Could not write {rel}: {exc}") from exc
    return _ok("resolve",
               f"Wrote the resolved {rel}. It is NOT staged and NOT committed — "
               "review it, then stage and commit as usual.",
               path=rel)


def _check_strings(op, paths, message, name, index, content=""):
    """Everything that can be decided WITHOUT touching the filesystem or git.

    Ordering, not just validation. `_locate` is itself a git call, so validating
    afterwards would mean a malformed argument had already caused a subprocess to
    run with the target's cwd. Cheap string checks come first, always — and for a
    module that writes, "first" is the whole point.
    """
    if op not in _OPS:
        raise _Refused("bad-op", f"Unknown operation: {op or '(none)'}")

    if op in _PATH_OPS:
        if not paths:
            raise _Refused("missing", "No paths were given for this operation.")
        if len(paths) > MAX_PATHS:
            raise _Refused(
                "too-many-paths",
                f"{len(paths)} paths in one request; this view sends at most "
                f"{MAX_PATHS}.")
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip():
                raise _Refused("bad-path", "A path was empty.")
            rel = raw.replace("\\", "/").strip("/")
            if not rel:
                raise _Refused("bad-path", "A path was empty.")
            # `--` already keeps a path out of the option position, so this is
            # defence in depth — but nothing this UI can produce starts with a
            # dash, so a name that does is a hand-written request and is treated
            # as one.
            if raw.startswith("-"):
                raise _Refused("bad-path", f"{raw} is not a path this view sends.")
            # On the STRING and before any I/O: `..` is not a pathspec error we
            # want to surface as a raw git failure, and an absolute path would
            # silently resolve outside the repository. This is NOT the whole
            # containment check — `_contain` needs the root, and `_in_scope`
            # needs the scope.
            if os.path.isabs(raw) or any(part == ".." for part in rel.split("/")):
                raise _Refused("outside-repo",
                               f"{raw} is outside the repository.")

    if op == "resolve":
        _check_resolve_content(paths, content)

    if op == "commit" and not (message or "").strip():
        # Our own refusal, not git's: `git commit -m ""` fails with "Aborting
        # commit due to empty commit message", which reads like something went
        # wrong rather than like "you left the box blank".
        raise _Refused("empty-message",
                       "Write a commit message before committing.")

    if op in ("branch_create", "branch_checkout", "branch_delete"):
        if not isinstance(name, str) or not name.strip():
            raise _Refused("bad-branch", "No branch name was given.")
        # THE rule that cannot be delegated to git: `check-ref-format` has no
        # `--` terminator, so a name starting with `-` would be parsed as one of
        # its own options — and so would it be by `switch` and `branch`.
        if name.startswith("-"):
            raise _Refused("bad-branch",
                           f"{name!r} is not a valid branch name.")

    if op in ("stash_apply", "stash_pop", "stash_drop"):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise _Refused("bad-index", "That is not a stash entry.")


def _contain(root, rel, full):
    """Containment for EVERY path argument: the realpath must be under root.

    `_check_strings`'s test is necessary and not sufficient — it proves the path
    NAMES nothing outside the repository, but the consumers of that name resolve
    it, so a symlink is how a repo-relative-looking name reaches outside. This
    also covers the case an `islink` test on the final component misses entirely:
    an ordinary file reached through a symlinked PARENT directory.

    log.py's twin, and load-bearing for a different reason here: there the cost
    of missing it is showing bytes from outside the repo, here it is DELETING
    them (`git clean` follows the resolved name).

    A path that does not exist is fine — a staged deletion names a file that is
    gone — because `realpath` resolves lexically past a missing leaf while still
    resolving every symlink that DOES exist above it.
    """
    real = os.path.realpath(full)
    if real != root and not real.startswith(root + os.sep):
        raise _Refused("outside-repo", f"{rel} resolves outside the repository.")


def _in_scope(rel, scope, scope_is_dir):
    """Whether a repo-relative path may be MUTATED from this view (GT-13).

    Stricter than the reader's `_in_scope`, and deliberately so. The reader
    LISTS an out-of-scope entry in one case — a rename with one side in the scope
    — because "this file left the folder you are looking at" is a change the view
    exists to show. Changing it is a different act: a view scoped to `pkg/` may
    not stage, discard or stash something under `docs/`, whatever the reason it
    appears in the list. So the ancestor case the reader honours is absent here
    too: a collapsed `dir/` row that is an ancestor of the scope covers files
    outside it, and discarding it would reach them.

    Branch checkout is exempt by nature and does not come through here: a branch
    is a repository concept, and there is no such thing as checking one out "just
    for pkg/".
    """
    if not scope:
        return True          # the whole repository is the scope
    if rel == scope:
        return True
    return scope_is_dir and rel.startswith(scope + "/")


def _resolve_paths(root, scope, scope_is_dir, paths):
    """Normalize, contain and scope-check each path; return the POSIX rels."""
    resolved = []
    for raw in paths:
        rel = raw.replace("\\", "/").strip("/")
        _contain(root, rel, os.path.join(root, *rel.split("/")))
        if not _in_scope(rel, scope, scope_is_dir):
            raise _Refused(
                "outside-scope",
                f"{rel} is outside {scope or 'this view'} — this view only "
                "changes what it shows.")
        resolved.append(rel)
    return resolved


def _repo_name(kind, value):
    """Refuse an option-shaped name that GIT produced, before it becomes argv.

    The rule `_check_strings` applies to a name the USER typed has to apply just
    as hard to a name git handed back, because git hands these back VERBATIM
    from files inside `.git`, and those files are content:

      * `git symbolic-ref --short HEAD` prints whatever `.git/HEAD` names. A
        hand-written `ref: refs/heads/-evil` yields the string `-evil`.
      * `git remote` prints the config section names. A hand-written
        `[remote "--upload-pack=<cmd>"]` yields `--upload-pack=<cmd>`.

    Neither is reachable through git's own porcelain — `git branch` and `git
    remote add` both reject these — so a repository containing one arrived some
    other way (a tarball, a zip, a shared drive; `git clone` does not copy
    config). That is a malformed or hostile repository, and the honest answer is
    to stop rather than to sanitize and continue: any name we "fixed" would no
    longer be the thing the user is looking at.

    `--` before every such value is the other half of this, and both halves are
    kept: `--` is the guarantee for the commands that have it, and this is the
    guarantee for the ones where a terminator does not exist or was forgotten.
    Depending on either alone is how one missing `--` becomes command execution.
    """
    if value and value.startswith("-"):
        raise _Refused(
            "unsafe-name",
            f"This repository's {kind} is named {value!r}, which git would read "
            "as a command-line option. Nothing here will run against it — the "
            "name almost certainly did not come from git itself.")
    return value


def _check_branch_name(root, name):
    """git decides whether a branch name is valid; we only ask.

    Exit 0 is "valid". Anything else is invalid, and git has already been told
    (by `_check_strings`) that the name does not start with a dash, which is the
    only input `check-ref-format` could otherwise misread as an option.
    """
    code, _, _ = _run(root, "check-ref-format", "--branch", name)
    if code != 0:
        raise _Refused("bad-branch", f"{name!r} is not a valid branch name.")


# --------------------------------------------------------------- the git facts


def _has_commits(root):
    code, _, _ = _run(root, "rev-parse", "--verify", "--quiet", "HEAD")
    return code == 0


def _current_branch(root):
    """The checked-out branch, or None when HEAD is detached.

    Guarded by `_repo_name`: this string is whatever `.git/HEAD` names, which is
    file CONTENT, so an option-shaped branch is a thing a repository can carry
    and every caller here feeds the result into argv.
    """
    _, out, _ = _run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return _repo_name("branch", out.decode("utf-8", "replace").strip()) or None


def _has_staged(root):
    """Whether anything is in the index that HEAD does not have.

    `git diff --cached --quiet` exits 1 when there ARE staged changes — an
    answer, not a failure — and works on an unborn HEAD too (it compares against
    the empty tree), which is exactly the case a `rev-parse HEAD` guard would
    have got wrong.
    """
    code, _, _ = _run(root, "diff", "--cached", "--quiet")
    return code == 1


def _tracked(root, rels):
    """Split `rels` into `(tracked, untracked)` as git sees them.

    Needed because "discard" is two different git commands: restoring a tracked
    file from the index, and deleting an untracked one. Asking `ls-files` is how
    that question gets answered by git rather than by a stat — a path can exist
    on disk and be untracked, or be tracked and already deleted.
    """
    out = _git_ok(root, "ls-files", "-z", *_spec(rels))
    known = {chunk.decode("utf-8", "replace")
             for chunk in out.split(b"\0") if chunk}
    tracked = [rel for rel in rels
               if rel in known or any(k.startswith(rel + "/") for k in known)]
    return tracked, [rel for rel in rels if rel not in tracked]


def _knows_anything(root, spec):
    """Whether git tracks any file under this pathspec.

    A probe rather than a fallback, because the commands that need it treat "the
    pathspec matched nothing tracked" as an ERROR rather than as an empty answer,
    and that error is wrong for every state this view can legitimately be in:

      * `git restore --worktree -- <spec>` exits 1 with "did not match any
        file(s) known to git" — so `discard_all` in a scope holding only
        UNTRACKED files (or in a repository with no commits) would fail, even
        though the whole job there belongs to the `clean` half that follows it.
      * `git rm --cached -- <spec>` exits 128 for the same situation, which is
        what `unstage_all` does before the first commit.

    The `rm` calls additionally carry `--ignore-unmatch` so an unstage of
    something that is not staged is a no-op rather than a failure — "it is
    already the way you asked for" is not an error a button should report.
    """
    out = _git_ok(root, "ls-files", "-z", *spec)
    return bool(out.strip(b"\0"))


def _stash_count(root):
    out = _git_ok(root, "stash", "list", "--format=%gd")
    return len([line for line in out.decode("utf-8", "replace").split("\n") if line])


def _remote_of(root, branch):
    """The remote to talk to: the branch's own, else the sole one, else origin.

    `branch.<name>.remote` is read through `git config --get`, which takes the
    key as ONE argv element — and the branch is interpolated into the MIDDLE of
    that key, so even a dash-leading branch could not make the key dash-leading.
    (It is refused upstream of here anyway; this is why it would not matter.)

    EVERY remote name is `_repo_name`-guarded, not just the one that gets picked.
    `git remote` echoes config section names verbatim, so a hand-written
    `[remote "--upload-pack=<cmd>"]` is a name this list can contain — and the
    whole list is refused rather than filtered, because a repository carrying one
    is malformed or hostile and silently using its other remote would hide that.
    """
    out = _git_ok(root, "remote")
    remotes = [_repo_name("remote", line.strip())
               for line in out.decode("utf-8", "replace").split("\n")
               if line.strip()]
    if branch:
        code, configured, _ = _run(root, "config", "--get",
                                   f"branch.{branch}.remote")
        named = configured.decode("utf-8", "replace").strip() if code == 0 else ""
        if named in remotes:
            return named, remotes
    if len(remotes) == 1:
        return remotes[0], remotes
    if "origin" in remotes:
        return "origin", remotes
    return None, remotes


def _require_remote(root, branch):
    remote, remotes = _remote_of(root, branch)
    if remote is None:
        raise _Refused(
            "no-remote",
            "This repository has no remote to talk to."
            if not remotes else
            "This repository has several remotes and no upstream for this "
            "branch, so there is no obvious one to use. Pick one in a terminal.")
    return remote


def _ok(op, detail, **extra):
    return {"ok": True, "op": op, "detail": detail, **extra}


def _n(count, singular, plural=None):
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


# ------------------------------------------------------------------- the ops


def _stage(root, rels, scope_label):
    # `add` and not `update-index`: it is the one verb that covers a modified
    # file, a new file and a deletion, which is what the UI's "+" means.
    _git_ok(root, "add", *_spec(rels))
    return _ok("stage", f"Staged {scope_label}.")


def _unstage(root, rels, scope_label):
    if _has_commits(root):
        _git_ok(root, "restore", "--staged", *_spec(rels))
    else:
        # Before the first commit there is no HEAD to restore FROM, and both
        # `restore --staged` and `reset -- <path>` exit 128 saying so. Removing
        # the entry from the index (keeping the file, `--cached`) is what
        # "unstage" means when the index has no baseline: the file goes back to
        # being untracked, which is exactly where it came from.
        _git_ok(root, "rm", "--cached", "-r", "--quiet", "--ignore-unmatch",
                *_spec(rels))
    return _ok("unstage", f"Unstaged {scope_label}.")


def _discard(root, rels, scope_label):
    """DESTRUCTIVE. Working-tree changes go away with no way back.

    Two commands because there are two situations, and using one for both is
    silently wrong in each direction: `restore --worktree` does nothing at all to
    an untracked file, and `clean` cannot restore a modified tracked one.

    `clean -fd` and never `-fdx`. `-x` additionally deletes IGNORED files, which
    is where `.env`, `node_modules` and virtualenvs live — a scale of loss
    completely unlike "throw away the edit I just made", and not something a
    confirmation dialog can meaningfully warn about because the files are, by
    construction, invisible in this view.
    """
    tracked, untracked = _tracked(root, rels)
    if tracked:
        _git_ok(root, "restore", "--worktree", *_spec(tracked))
    if untracked:
        _git_ok(root, "clean", "-fd", *_spec(untracked))
    return _ok("discard", f"Discarded changes in {scope_label}.")


def _commit(root, message):
    if not _has_staged(root):
        # Our own refusal. `git commit` with an empty index exits 1 and prints
        # the whole status as advice, which in a GUI is a wall of text answering
        # a question nobody asked.
        raise _Refused(
            "nothing-staged",
            "Nothing is staged. Stage a change first — a commit records the "
            "index, not the working tree.")
    # `-m <message>`: ONE argv element, so the message may hold newlines, quotes,
    # backticks, a `$(...)` — there is no shell for any of it to mean anything
    # to. And no pathspec, ever: `git commit -- <paths>` would bypass the index
    # and record the working tree for those paths (GT-14).
    _git_ok(root, "commit", "-m", message)
    out = _git_ok(root, "log", "-1", "--no-color", f"--format={_COMMIT_FORMAT}")
    parts = out.decode("utf-8", "replace").strip().split("\0")
    short, subject = (parts + ["", ""])[:2]
    return _ok("commit", f"Committed {short}.", short=short, subject=subject)


def _branch_create(root, name, checkout):
    _check_branch_name(root, name)
    if checkout:
        # `switch -c` creates and checks out in one step, so there is no window
        # in which the branch exists but HEAD is elsewhere.
        _switch(root, "-c", name)
        return _ok("branch_create", f"Created and switched to {name}.",
                   branch=name)
    _git_ok(root, "branch", "--", name)
    return _ok("branch_create", f"Created {name}.", branch=name)


def _switch(root, *args):
    """`git switch`, falling back to `git checkout` on a git older than 2.23.

    `switch` is preferred because it does one thing: it cannot be talked into
    restoring files, which is `checkout`'s other, path-shaped meaning and the
    reason `checkout` is easy to misuse. On a git without it, the fallback is
    `checkout` with the SAME arguments — `-c` becomes `-b`, the only difference
    between the two spellings for these two calls.

    Detected by exit code + message rather than by parsing `git --version`: a
    version string is another format to get wrong, and the answer we actually
    need is "did this git understand the verb", which the attempt itself gives.
    """
    code, _, err = _run(root, "switch", *args)
    if code == 0:
        return
    if "is not a git command" in err.lower() or "unknown option" in err.lower():
        legacy = ["-b" if a == "-c" else a for a in args]
        _git_ok(root, "checkout", *legacy)
        return
    # Any other failure is git refusing for a real reason ("Your local changes
    # would be overwritten…"), and its sentence is the one to show.
    raise _Refused("git-failed", _brief(err) or f"git exited {code}.")


def _branch_checkout(root, name):
    _check_branch_name(root, name)
    _switch(root, name)
    return _ok("branch_checkout", f"Switched to {name}.", branch=name)


def _branch_delete(root, name):
    _check_branch_name(root, name)
    # `-d`, never the capital. `-d` refuses to delete a branch whose commits are
    # not reachable from anywhere else, and that refusal — surfaced verbatim — is
    # the whole safety property: the one thing a GUI must not make easy is
    # throwing away commits.
    _git_ok(root, "branch", "-d", "--", name)
    return _ok("branch_delete", f"Deleted {name}.", branch=name)


def _stash_push(root, rel, message, include_untracked):
    args = ["stash", "push"]
    if include_untracked:
        args.append("-u")
    if (message or "").strip():
        args += ["-m", message]
    # Scoped by pathspec exactly like every other write here. At the repository
    # root there is no pathspec at all rather than a bare `--`: `--` with nothing
    # after it is the EMPTY path list and not "everything" — the same trap
    # `_scope_spec` exists for, and true of current git, not only of old git.
    if rel:
        args += _pathspec(rel)
    code, out, err = _run(root, *args)
    if code != 0:
        raise _Refused("git-failed", _brief(err) or f"git exited {code}.")
    if _NOTHING_TO_STASH in _said(out, err):
        # git exits 0 for this, so the exit code cannot carry it. Reported as a
        # refusal because from the UI it is indistinguishable from a no-op, and a
        # button that silently does nothing is a bug report. Read across BOTH
        # streams: today it lands on stdout, and the day that changes this guard
        # would disappear without a sound.
        raise _Refused("nothing-to-stash",
                       "There is nothing to stash under this path.")
    return _ok("stash_push", "Stashed the changes under this path.")


def _stash_at(root, index, sha):
    """`stash@{n}`, once n is known to name the entry the CALLER meant.

    A position is not an identity. `stash@{n}` means "the nth entry of the stash
    reflog *right now*", and every `stash push` — from this view, from another
    tab, from a terminal — shifts every index by one. Between the moment a row is
    drawn (or a confirmation is asked, which can sit in the URL indefinitely) and
    the moment the call lands, a merely bounds-checked index can therefore
    address a DIFFERENT, still-wanted entry — and for `drop` that loss has no
    undo. Bounds-checking answers "does something exist there", which is not the
    question.

    So the caller quotes back the commit id the reader gave it (`log.py`'s
    `_stashes` puts `%H` on every entry) and this verifies it. A mismatch is a
    refusal telling the user to look again, deliberately NOT a "find that sha and
    use its real index" repair: the list they were reading is stale, and silently
    acting on a redrawn one is the same class of surprise in a new costume.

    The bound check stays ours rather than git's because git's own message for a
    missing entry is `fatal: log for 'refs/stash' only has 2 entries`, which is
    plumbing talking. `n` is already an int, so the formatted ref cannot be
    anything but a stash reference.
    """
    count = _stash_count(root)
    if index >= count:
        raise _Refused("no-such-stash",
                       f"There is no stash@{{{index}}} — this repository has "
                       f"{_n(count, 'stash', 'stashes')}.")
    ref = f"stash@{{{index}}}"
    if not _SHA_RE.match(sha or ""):
        raise _Refused("bad-stash-id",
                       "That request did not say which stash entry it meant.")
    # `^{commit}` so the comparison is against the entry's commit id whatever the
    # reflog holds, and `--verify --quiet` so a vanished entry is exit 1 rather
    # than a failure payload — it is the same "the list moved" answer.
    actual = _git_ok(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}",
                     allow=(0, 1)).decode("utf-8", "replace").strip()
    if not actual or not actual.startswith(sha):
        raise _Refused(
            "stash-moved",
            f"{ref} is not the stash entry this was about any more — the list "
            "changed since it was read. Check the stashes again.")
    return ref


def _stash_apply(root, index, sha, pop):
    ref = _stash_at(root, index, sha)
    # `pop` = apply + drop, and git does it atomically: a conflicting apply
    # leaves the entry in place rather than dropping work that did not land.
    _git_ok(root, "stash", "pop" if pop else "apply", ref)
    return _ok("stash_pop" if pop else "stash_apply",
               f"{'Popped' if pop else 'Applied'} {ref}.")


def _stash_drop(root, index, sha):
    """DESTRUCTIVE. The stashed work is gone (bar the reflog, which is not a UI)."""
    ref = _stash_at(root, index, sha)
    _git_ok(root, "stash", "drop", ref)
    return _ok("stash_drop", f"Dropped {ref}.")


def _fetch(root):
    branch = _current_branch(root)
    remote = _require_remote(root, branch)
    # `--prune` so a branch deleted on the remote stops being listed here. It
    # only ever removes remote-TRACKING refs; no local branch and no object a
    # local branch needs is touched by it.
    _git_ok(root, "fetch", "--prune", "--", remote)
    return _ok("fetch", f"Fetched from {remote}.")


def _upstream_target(root, remote, branch):
    """`(remote-side branch name, whether an upstream is recorded)`.

    Both network calls name their refspec explicitly (see `_push`), and the local
    and remote names are allowed to differ — plain `git push` honours the
    recorded mapping, so an explicit form that assumed the names matched would
    quietly push somewhere else. With no upstream, the branch's own name is the
    only defensible target, which is also what `--set-upstream` would record.
    """
    code, out, _ = _run(root, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{upstream}")
    upstream = out.decode("utf-8", "replace").strip() if code == 0 else ""
    if upstream.startswith(remote + "/"):
        return upstream[len(remote) + 1:], True
    return branch, bool(upstream)


def _pull(root):
    branch = _current_branch(root)
    if not branch:
        raise _Refused("detached",
                       "HEAD is detached, so there is nothing to pull into. "
                       "Check out a branch first.")
    remote = _require_remote(root, branch)
    target, _ = _upstream_target(root, remote, branch)
    # Explicit remote and refspec, for the same reason `_push` is explicit: a
    # bare `git pull` takes its target from `branch.<name>.merge` and its style
    # from `pull.rebase`, so what this button did would be decided by config the
    # view never shows. `--ff-only` bounds the OUTCOME; naming the refspec bounds
    # the INPUT, and both are wanted.
    code, out, err = _run(root, "pull", "--ff-only", "--", remote, target)
    if code == 0:
        return _ok("pull", f"Pulled {remote}/{target} — the branch fast-forwarded.")
    if _NOT_FF in _said(out, err) or "diverge" in _said(out, err):
        # A divergence is a decision, not an error. Both automatic answers are
        # wrong to take on someone's behalf: a merge writes a commit they did not
        # ask for, a rebase rewrites commits they already have. So this stops.
        raise _Refused(
            "not-fast-forward",
            "Your branch and its upstream have diverged, so this cannot "
            "fast-forward. Merging or rebasing is a decision this view will not "
            "make for you — do it in a terminal.")
    raise _Refused("git-failed", _brief(err) or f"git exited {code}.")


def _push(root):
    branch = _current_branch(root)
    if not branch:
        raise _Refused("detached",
                       "HEAD is detached, so there is no branch to push. "
                       "Check out a branch first.")
    remote = _require_remote(root, branch)
    target, has_upstream = _upstream_target(root, remote, branch)
    if has_upstream:
        # The remote and the refspec are NAMED, and `--` comes before them. A
        # bare `git push` is the one command in this module whose meaning is
        # decided by config the view never shows: under `push.default=matching`
        # it pushes every matching local branch, and under `remote.pushDefault`
        # it pushes to a different remote than the success message names — in
        # both cases doing more, or something else, than the button said.
        # `HEAD:refs/heads/<target>` is "this branch, to where it tracks",
        # fully qualified so no remote ref of another kind can be matched, and
        # prefixed by `HEAD:` so no part of it can be option-shaped whatever the
        # refnames in this repository are.
        _git_ok(root, "push", "--", remote, "HEAD:refs/heads/" + target)
        return _ok("push", f"Pushed to {remote}/{target}.")
    # No upstream recorded. git's own answer here is advice ("use --set-upstream
    # to push and track"), which is a sentence a GUI button cannot act on — so
    # this DOES set it. That is a deliberate decision and a narrow one: setting
    # an upstream can only ever create a remote branch that does not exist yet,
    # it is not a force, and it cannot overwrite anyone's work. If the remote
    # branch does exist and has commits we do not have, git refuses exactly as it
    # would for any other non-fast-forward push, and that refusal is shown.
    #
    # `--` before the remote and the refspec, exactly as above, and for a reason
    # that is not style: both values are REPO-DERIVED (`git remote` echoes config
    # section names, `symbolic-ref` echoes `.git/HEAD`), so a hand-written
    # `[remote "--receive-pack=<cmd>"]` would otherwise turn this one click into
    # local command execution. `_repo_name` already refuses such a name, and the
    # terminator is kept anyway: two independent guarantees, because depending on
    # either alone is exactly how a single missing `--` becomes that bug.
    _git_ok(root, "push", "--set-upstream", "--", remote,
            "HEAD:refs/heads/" + branch)
    return _ok("push", f"Published {branch} to {remote}.")


# ------------------------------------------------------------------ dispatch


def main(
    file: str,
    op: str = "",
    paths: "list[str] | None" = None,
    message: str = "",
    name: str = "",
    index: int = -1,
    include_untracked: bool = False,
    checkout: bool = True,
    sha: str = "",
    content: str = "",
) -> dict:
    """One mutation, or a refusal payload.

    `op` has no default that DOES anything: there is no safe default mutation, so
    an omitted op is a bug in the caller rather than a request to be interpreted.
    """
    try:
        # Strings first, always — `_locate` forks git, so nothing malformed may
        # get that far.
        _check_strings(op, paths, message, name, index, content)
        root, scope, scope_is_dir = _locate(file)

        if op in _PATH_OPS:
            rels = _resolve_paths(root, scope, scope_is_dir, paths)
            label = _n(len(rels), "path")
            if op == "resolve":
                return _resolve(root, rels[0], content)
            if op == "stage":
                return _stage(root, rels, label)
            if op == "unstage":
                return _unstage(root, rels, label)
            return _discard(root, rels, label)

        if op in _SCOPE_OPS:
            spec = _scope_spec(scope)
            label = scope or "this repository"
            if op == "stage_all":
                _git_ok(root, "add", *spec)
                return _ok("stage_all", f"Staged everything in {label}.")
            if op == "unstage_all":
                if _has_commits(root):
                    if _knows_anything(root, spec):
                        _git_ok(root, "restore", "--staged", *spec)
                else:
                    _git_ok(root, "rm", "--cached", "-r", "--quiet",
                            "--ignore-unmatch", *spec)
                return _ok("unstage_all", f"Unstaged everything in {label}.")
            # discard_all — DESTRUCTIVE, and the one place the scope pathspec
            # goes to `clean`. Still no `-x`: an ignored file is never in scope
            # for a discard, whatever the scope is.
            #
            # The restore is SKIPPED when git tracks nothing here rather than
            # allowed to fail: a scope holding only untracked files is ordinary,
            # and there the `clean` below is the entire operation.
            if _knows_anything(root, spec):
                _git_ok(root, "restore", "--worktree", *spec)
            _git_ok(root, "clean", "-fd", *spec)
            return _ok("discard_all", f"Discarded all changes in {label}.")

        if op == "commit":
            return _commit(root, message)
        if op == "branch_create":
            return _branch_create(root, name, bool(checkout))
        if op == "branch_checkout":
            return _branch_checkout(root, name)
        if op == "branch_delete":
            return _branch_delete(root, name)
        if op == "stash_push":
            return _stash_push(root, scope, message, bool(include_untracked))
        if op == "stash_apply":
            return _stash_apply(root, index, sha, pop=False)
        if op == "stash_pop":
            return _stash_apply(root, index, sha, pop=True)
        if op == "stash_drop":
            return _stash_drop(root, index, sha)
        if op == "fetch":
            return _fetch(root)
        if op == "pull":
            return _pull(root)
        return _push(root)
    except _Refused as refused:
        return refused.payload


# The fused-render engine / app runner only invoke a @fused.udf-registered
# entrypoint; a bare main() returns null under them. Register main via the shim
# (the house pattern — canvas/las/usd readers, and log.py beside this file) so it
# runs under the engine, while `main` stays a plain callable for the built-in
# executor and for tests.
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
