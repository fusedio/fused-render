"""CLAUDE.md explorer (claude-md.md).

Finds every CLAUDE.md-family file on the machine and lets the page preview,
edit, and delete them. Discovery unions three sources, because no single one
sees everything:

  1. Spotlight (`mdfind -name`) — whole-disk, but skips hidden dirs, so it
     can never see ~/.claude/CLAUDE.md. macOS only.
  2. The `projects` keys of ~/.claude.json — every directory Claude Code has
     run in; each is probed for CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md.
  3. The global ~/.claude/CLAUDE.md, explicitly.

File CONTENT is read/written by the page via fused.readFile/writeFile (no
Python round-trip); this module only discovers, deletes, and reveals.

main(action=...):
  list   -> {files: [{path, dir, name, size, mtime, empty, scope}], engine}
  delete -> {ok, committed?}  params: path  (basename-allowlisted)
  open   -> {ok}              params: path  (reveal in OS explorer)
"""
import json
import os
import subprocess
import sys

from . import lib

NAMES = ("CLAUDE.md", "CLAUDE.local.md")
# Directories nobody means when they ask "show me my CLAUDE.mds".
NOISE = ("/node_modules/", "/.Trash/", "/Library/Caches/")


def _mdfind() -> list:
    """Spotlight hits for each exact name; [] off-macOS or on any failure."""
    if sys.platform != "darwin":
        return []
    hits = []
    for name in NAMES:
        try:
            out = subprocess.run(
                ["mdfind", "-name", name],
                # lib.SUBPROCESS_KWARGS, not text=True: mdfind prints filesystem paths,
                # and one accented directory name anywhere on the disk would
                # otherwise ASCII-decode-fail the whole MD Files listing.
                capture_output=True, timeout=10, **lib.SUBPROCESS_KWARGS,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        # -name substring-matches, so CLAUDE.local.md also comes back for
        # "CLAUDE.md" — the exact-basename filter dedupes that.
        hits += [p for p in out.splitlines() if os.path.basename(p) == name]
    return hits


def _project_dirs() -> list:
    """Directories Claude Code has run in, per ~/.claude.json (may be gone)."""
    state = lib.read_json(os.path.expanduser("~/.claude.json"), {})
    projects = state.get("projects")
    return sorted(projects) if isinstance(projects, dict) else []


def _candidates() -> dict:
    """path -> scope for every file worth showing (dedup by realpath)."""
    found = {}

    def add(path: str, scope: str):
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return
        if any(n in real for n in NOISE):
            return
        found.setdefault(real, scope)

    add(os.path.join(lib.CLAUDE_DIR, "CLAUDE.md"), "global")
    for d in _project_dirs():
        for name in NAMES:
            add(os.path.join(d, name), "project")
        add(os.path.join(d, ".claude", "CLAUDE.md"), "project")
    for p in _mdfind():
        add(p, "disk")
    return found


def _list() -> dict:
    files = []
    for path, scope in _candidates().items():
        try:
            st = os.stat(path)
            text = (lib.read_text(path) or "").strip()
            empty = st.st_size == 0 or text == ""
        except OSError:
            continue
        files.append({
            "path": path,
            "dir": os.path.dirname(path),
            "name": os.path.basename(path),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "empty": empty,
            "scope": scope,
            # First few lines for the card preview — enough to recognise the
            # file at a glance without opening it. Character-capped so a
            # pathological one-line file can't ship kilobytes into the list.
            "snippet": "\n".join(text.splitlines()[:6])[:400],
        })
    files.sort(key=lambda f: (f["scope"] != "global", f["dir"].lower(), f["name"].lower()))
    engine = "spotlight+claude.json" if sys.platform == "darwin" else "claude.json"
    return {"files": files, "engine": engine}


def _validated(path: str) -> str:
    """Absolute, existing, CLAUDE.md-named file — the only ones we touch."""
    real = os.path.realpath(path)
    if os.path.basename(real) not in NAMES:
        raise ValueError(f"refusing to act on {os.path.basename(real)!r}: not a CLAUDE.md file")
    if not os.path.isfile(real):
        raise ValueError(f"not a file: {real}")
    return real


def main(action: str = "list", path: str = "") -> dict:
    if action == "list":
        return _list()

    try:
        real = _validated(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if action == "open":
        lib.reveal(os.path.dirname(real))
        return {"ok": True}

    if action == "commit":
        # Fold an already-written edit into the config repo's history. The
        # page saves file content through the shell's own /api/fs/write (this
        # module never handles content), so a save landing inside ~/.claude
        # would otherwise sit as uncommitted drift with nothing on the page
        # surfacing it. Outside the config repo there is nothing to commit —
        # a no-op, not an error, so the caller doesn't need to know where the
        # boundary is.
        claude_root = os.path.realpath(lib.CLAUDE_DIR) + os.sep
        if not real.startswith(claude_root):
            return {"ok": True, "committed": None}
        rel = real[len(claude_root):]
        with lib.config_lock():
            committed = lib.commit(f"Edit {rel}", pathspec=rel)
        return {"ok": True, "committed": committed}

    if action == "delete":
        # Inside the config repo the deletion is committed like any other
        # config change; project files live in their own repos — plain unlink.
        claude_root = os.path.realpath(lib.CLAUDE_DIR) + os.sep
        if real.startswith(claude_root):
            rel = real[len(claude_root):]
            with lib.config_lock():
                os.remove(real)
                committed = lib.commit(f"Delete {rel}", pathspec=rel)
            return {"ok": True, "committed": committed}
        os.remove(real)
        return {"ok": True}

    return {"ok": False, "error": f"unknown action: {action}"}
