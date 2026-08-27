"""Memory feature (memory.md).

Read-only viewer of Claude Code's persistent memory under
projects/*/memory/*.md, grouped by project, with per-folder git lifecycle
controls (change status, commit, clear). Memory *contents* are authored by
Claude Code, never edited here.

main(action=...):
  list   -> {projects: [{project, path, pathConfirmed, files, changes}]}
            files: [{name, description}], description from the file's own
            YAML frontmatter (None when missing or malformed — never guessed).
  commit -> {ok, committed}   params: project  (path-limited commit)
  clear  -> {ok, committed}   params: project  (delete *.md + commit deletion)
  open   -> {ok}              params: project  (reveal folder in OS explorer)

`project` is always the SLUG (the projects/ directory name). It stays the
identifier on the wire because it is what lib.safe_subdir guards and what the
other three actions are keyed on; `path` is the human-readable folder it stands
for, resolved as described under _project_path.
"""
import glob
import json
import os
import re
from typing import Optional

from . import lib

PROJECTS_DIR = os.path.join(lib.CLAUDE_DIR, "projects")

# How many "-"-separated segments a slug may have before we stop trying to
# reconstruct a path out of it. The rejoin search below backtracks, so it is
# exponential in the worst case and lists a directory per level; real project
# paths are ~5-15 segments deep, and a pathological slug is not worth the walk.
_MAX_SEGMENTS = 24


# --- slug -> real folder ----------------------------------------------------
# Claude Code names each project dir by munging the cwd:
# re.sub(r"[^A-Za-z0-9]", "-", abspath) — see templates/claude/agent.py's
# _munge(). That is LOSSY and irreversible: "/", ".", "_" and a literal "-" all
# collapse to "-". So a "-" -> "/" replace turns
# "-Users-me-Work-fused-render" into "/Users/me/Work/fused/render", a path that
# does not exist. Rendering a confidently-wrong path in a UI over someone's
# dotfiles is worse than rendering the slug, so this never guesses:
#
#   1. read the truth out of a transcript's `cwd` (what
#      server/routers/claude_sessions.py does — see the note on _transcript_cwd);
#   2. failing that, reconstruct against the filesystem, keeping only a
#      candidate whose every component actually exists;
#   3. failing that, return None, and the UI shows the slug.


def _transcript_cwd(slug_dir: str) -> Optional[str]:
    """The `cwd` recorded in one of this project's transcripts, or None.

    A local mirror of server/routers/claude_sessions.py's _session_cwd() —
    duplicated rather than imported because this package must not depend on the
    server's routers (it is callable standalone, as an html+py app was). If the
    transcript format ever changes, both need the change; that is the cost of
    the layering, stated here so the two don't drift silently.

    Stops at the first line carrying a `cwd`, which is normally the first line —
    a transcript can be many MB.
    """
    for jsonl in sorted(glob.glob(os.path.join(slug_dir, "*.jsonl"))):
        try:
            with open(jsonl, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def _munge(name: str) -> str:
    """Claude Code's own transform, applied to one path component.
    templates/claude/agent.py::_munge does this to the whole abspath."""
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def _rejoin(parts: list, base: str) -> Optional[str]:
    """Rebuild a path from munged segments, guided by what exists on disk.

    Matches MUNGED-TO-MUNGED against the real directory entries rather than
    trying to un-munge: the transform is many-to-one, but it is cheap to apply,
    so listing `base` and munging each entry says which one the segment came
    from without ever guessing. That is what recovers a component the naive
    direction cannot — ".openfused" munges to "-openfused", so splitting the
    slug yields an EMPTY segment followed by "openfused" and no amount of
    rejoining with "-" produces the dot back.

    Fewest segments first, extending only when that doesn't resolve (which is
    how "fused", "render" becomes "fused-render"), and backtracking, because a
    shorter prefix that happens to match can still be a dead end further down.
    """
    if not parts:
        return base
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for take in range(1, len(parts) + 1):
        want = "-".join(parts[:take])
        for entry in entries:
            if _munge(entry) != want:
                continue
            candidate = os.path.join(base, entry)
            if not os.path.isdir(candidate):
                continue
            found = _rejoin(parts[take:], candidate)
            if found:
                return found
    return None


def _project_path(slug: str) -> Optional[str]:
    """The real folder a project slug stands for, or None if we can't confirm
    one. Never returns a path that was merely plausible."""
    cwd = _transcript_cwd(os.path.join(PROJECTS_DIR, slug))
    # A recorded cwd is the truth even if the folder has since been deleted:
    # it is where those sessions ran, not a guess we are making now.
    if cwd:
        return cwd
    # A munged absolute path carries its root as a recognizable prefix: POSIX's
    # leading "/" munges to a leading "-", while Windows' "C:\" munges to a
    # leading "C--" (the drive letter survives — it's alnum — then ":" and the
    # first "\" each become their own "-"). Without one of these there is no
    # anchor to walk from.
    drive_match = re.match(r"^([A-Za-z])--", slug)
    if drive_match:
        base = drive_match.group(1) + ":" + os.sep
        rest = slug[drive_match.end():]
    elif slug.startswith("-"):
        base = os.sep
        rest = slug[1:]
    else:
        return None
    parts = rest.split("-") if rest else []
    if not parts or len(parts) > _MAX_SEGMENTS:
        return None
    return _rejoin(parts, base)


def _memory_dir(project: str) -> str:
    """Validated projects/<slug>/memory path (traversal-guarded, must exist)."""
    return lib.safe_subdir(PROJECTS_DIR, project, "memory")


# How many lines of a candidate frontmatter block to read before giving up on
# finding the closing `---`. Claude Code's own frontmatter (name, description,
# metadata + a few nested keys) is well under this; a file that doesn't close
# within it is either not frontmatter or malformed either way, and this caps
# reading a large memory file line-by-line just to decide that.
_FRONTMATTER_SCAN_LIMIT = 40

_DESCRIPTION_RE = re.compile(r"^description:\s*(.*?)\s*$")


def _file_description(path: str) -> Optional[str]:
    """The one-line `description:` value from a memory file's YAML frontmatter,
    or None. Never invents one — a missing, unparsed or malformed frontmatter
    block all fall back to None, which the UI renders as the bare filename
    rather than guessing at what the file is about.

    Deliberately not a real YAML parser: Claude Code's own frontmatter is a
    flat `key: value` block (plus one nested `metadata:` map this doesn't need
    to read), and pulling in a YAML dependency for one scalar field is not
    worth it. `description` is read as plain text with matching outer quotes
    stripped, which is exactly what Claude Code itself writes.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            if f.readline().rstrip("\n") != "---":
                return None
            block = []
            closed = False
            for _ in range(_FRONTMATTER_SCAN_LIMIT):
                line = f.readline()
                if not line:
                    break  # EOF before a closing --- : not frontmatter.
                if line.rstrip("\n") == "---":
                    closed = True
                    break
                block.append(line)
    except OSError:
        return None
    # An unterminated block is never real frontmatter — collecting a
    # description out of it would be reading arbitrary file content as if it
    # were structured, which is exactly the "plausible but wrong" failure mode
    # this whole function exists to avoid.
    if not closed:
        return None
    for line in block:
        m = _DESCRIPTION_RE.match(line)
        if not m:
            continue
        value = m.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _list() -> dict:
    projects = []
    if os.path.isdir(PROJECTS_DIR):
        changes = _memory_changes()
        for slug in sorted(os.listdir(PROJECTS_DIR)):
            mem_dir = os.path.join(PROJECTS_DIR, slug, "memory")
            if not os.path.isdir(mem_dir):
                continue
            names = [n for n in os.listdir(mem_dir) if n.endswith(".md")]
            if not names:
                continue
            # MEMORY.md first, then alphabetical
            names.sort(key=lambda n: (n != "MEMORY.md", n.lower()))
            files = [
                {"name": n, "description": _file_description(os.path.join(mem_dir, n))}
                for n in names
            ]
            path = _project_path(slug)
            projects.append({
                "project": slug,
                "path": path,
                # Redundant with `path is not None` on purpose: it is the wire's
                # statement of the rule ("we only ever send a folder we could
                # confirm"), so a client branches on a named flag instead of
                # inferring policy from a null.
                "pathConfirmed": path is not None,
                "files": files,
                "changes": changes.get(slug, []),
            })
    return {"projects": projects}


def _memory_changes() -> dict:
    """Per-folder uncommitted change status, grouped by project slug
    (memory.md §7). Same porcelain source as the status badge."""
    try:
        out = lib.git("status", "--porcelain", "-uall")
    except RuntimeError:
        return {}
    by_project = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        st = "A" if code.strip() == "??" else code.strip()[0]
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "memory":
            by_project.setdefault(parts[1], []).append({"path": path, "status": st})
    return by_project


def main(action: str = "list", project: str = "") -> dict:
    if action == "list":
        return _list()

    if action == "open":
        try:
            lib.reveal(_memory_dir(project))
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if action == "commit":
        _memory_dir(project)  # validate slug + ensure dir exists
        rel = os.path.join("projects", project, "memory")
        with lib.config_lock():
            committed = lib.commit(f"Update memory for {project}", pathspec=rel)
        return {"ok": True, "committed": committed}

    if action == "clear":
        mem_dir = _memory_dir(project)
        rel = os.path.join("projects", project, "memory")
        with lib.config_lock():
            for n in os.listdir(mem_dir):
                if n.endswith(".md"):
                    os.remove(os.path.join(mem_dir, n))
            committed = lib.commit(f"Clear memory for {project}", pathspec=rel)
        return {"ok": True, "committed": committed}

    return {"ok": False, "error": f"unknown action: {action}"}
