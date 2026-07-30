import os
import subprocess
import sys
import tempfile




# Lazily-created empty git dir backing check-ignore for NON-repo directories
# that still carry a .gitignore (an un-inited project, an Obsidian vault…).
# With GIT_DIR pointing here and GIT_WORK_TREE at the directory, git applies
# that tree's .gitignore files exactly as it would inside a real repo. One
# per process, a few KB, left for the OS tempdir cleanup.
_EMPTY_GIT_DIR: str | None | bool = None  # None = not tried, False = failed


def _empty_git_dir():
    global _EMPTY_GIT_DIR
    if _EMPTY_GIT_DIR is None:
        try:
            root = tempfile.mkdtemp(prefix="fused-render-emptygit-")
            subprocess.run(
                ["git", "init", "-q", root],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                # close_fds=False → posix_spawn, skipping PROJ's crashing
                # atfork handler; see executor.py's run call for the full
                # story. Same on every spawn in this file.
                close_fds=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            _EMPTY_GIT_DIR = os.path.join(root, ".git")
        except (OSError, subprocess.SubprocessError):
            _EMPTY_GIT_DIR = False
    return _EMPTY_GIT_DIR or None


class _IgnoreOracle:
    """One repository's `git check-ignore` as a streaming co-process.

    `git check-ignore --stdin -z -v -n` answers path queries incrementally on
    one long-lived subprocess (measured ~14µs/query), so the walk can ask
    about every directory's children as it reaches them — no subprocess per
    directory, no giant upfront batch. `-v -n` makes git echo all four
    NUL-terminated fields for EVERY query (matching or not), which is what
    makes the stream pairable: query order in = verdict order out.

    Any failure (git missing, repo gone mid-walk, pipe breakage) marks the
    oracle broken and it answers "nothing ignored" from then on — gitignore
    pruning is an optimization, never a hard dependency (same posture as
    _git_ignored's dimming).
    """

    # Queries per write/read cycle: bounded so git's stdout can't fill the
    # pipe while we are still writing stdin (classic co-process deadlock).
    CHUNK = 200

    def __init__(self, repo_root):
        self.root = repo_root
        self.broken = False
        # Real repo (a .git exists at or above the root): plain `git -C`.
        # Standalone-.gitignore directory (no repo): graft the dir onto a
        # shared empty GIT_DIR as its work tree, which makes check-ignore
        # honor the tree's .gitignore files without a repository.
        env = None
        if not os.path.exists(os.path.join(repo_root, ".git")):
            empty = _empty_git_dir()
            if empty is None:
                self.proc = None
                self.broken = True
                self._buf = b""
                return
            env = {**os.environ, "GIT_DIR": empty, "GIT_WORK_TREE": repo_root}
        try:
            self.proc = subprocess.Popen(
                ["git", "-C", repo_root, "check-ignore", "--stdin", "-z", "-v", "-n"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=False,  # posix_spawn, not fork — see _empty_git_dir
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError:
            self.proc = None
            self.broken = True
        self._buf = b""

    def _read_field(self):
        while True:
            cut = self._buf.find(b"\0")
            if cut != -1:
                field = self._buf[:cut]
                self._buf = self._buf[cut + 1:]
                return field
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                raise OSError("check-ignore stream closed")
            self._buf += chunk

    def ignored(self, rel_paths):
        """Subset of `rel_paths` (POSIX, relative to repo root) git ignores."""
        if self.broken or not rel_paths:
            return set()
        out = set()
        try:
            for i in range(0, len(rel_paths), self.CHUNK):
                chunk = rel_paths[i : i + self.CHUNK]
                payload = b"".join(os.fsencode(r) + b"\0" for r in chunk)
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                for r in chunk:
                    # <source> NUL <linenum> NUL <pattern> NUL <path> NUL.
                    # Empty source = no pattern matched (not ignored). A
                    # NEGATED pattern ("!keep.log") is also echoed with its
                    # source under -v — that match means explicitly NOT
                    # ignored, so test the pattern's sign, not mere presence.
                    source = self._read_field()
                    self._read_field()
                    pattern = self._read_field()
                    self._read_field()
                    if source and not pattern.startswith(b"!"):
                        out.add(r)
            return out
        except OSError:
            self.broken = True
            self.close()
            return set()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            self.proc = None


def _repo_toplevel(path):
    """The git work-tree root containing `path`, or None. One call per walk —
    covers walking a SUBDIRECTORY of a repo, where no `.git` marker is ever
    seen during the walk itself."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            close_fds=False,  # posix_spawn, not fork — see _empty_git_dir
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = os.fsdecode(proc.stdout.strip())
    return top or None


def _git_ignored(cwd: str, rel_names: list[str]) -> set[str]:
    """Return the subset of `rel_names` git would ignore, relative to `cwd`.

    Shells out to `git check-ignore` — the authority on gitignore semantics
    (nested .gitignore, .git/info/exclude, the global excludesfile, negation).
    One batched call covers a whole listing. Returns an empty set when `cwd`
    is not in a work tree, git is missing, or anything else goes wrong:
    dimming is a display hint, never a hard dependency on git.

    The `.git` directory (or the gitfile of a worktree/submodule) is folded in
    too: git never reports it via check-ignore, but it is repository plumbing
    the user rarely wants to browse, so we dim it exactly when we know git is
    present and `cwd` is a work tree — i.e. only after a successful call.
    """
    if not rel_names:
        return set()
    try:
        # --stdin -z: NUL-separated in and out, so names with newlines or
        # non-UTF-8 bytes round-trip intact. check-ignore exits 0 when some
        # path is ignored, 1 when none are (not an error), 128 on real
        # failure incl. "not a git repository".
        payload = b"".join(os.fsencode(n) + b"\0" for n in rel_names)
        proc = subprocess.run(
            ["git", "-C", cwd, "check-ignore", "--stdin", "-z"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            close_fds=False,  # posix_spawn, not fork — see _empty_git_dir
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    ignored = {os.fsdecode(chunk) for chunk in proc.stdout.split(b"\0") if chunk}
    # Return code 0/1 (not 128) proves this is a work tree with git available,
    # so dim `.git` itself. Match the basename so both the top-level entry
    # (".git", from /api/fs/list) and a nested one ("sub/.git", from walk) go.
    ignored.update(n for n in rel_names if n == ".git" or n.endswith("/.git"))
    return ignored
