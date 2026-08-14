"""Memory feature (memory.md).

Read-only viewer of Claude Code's persistent memory under
projects/*/memory/*.md, grouped by project, with per-folder git lifecycle
controls (change status, commit, clear). Memory *contents* are authored by
Claude Code, never edited here.

main(action=...):
  list   -> {projects: [{project, files, changes}]}
  commit -> {ok, committed}   params: project  (path-limited commit)
  clear  -> {ok, committed}   params: project  (delete *.md + commit deletion)
  open   -> {ok}              params: project  (reveal folder in OS explorer)
"""
import os

from . import lib

PROJECTS_DIR = os.path.join(lib.CLAUDE_DIR, "projects")


def _memory_dir(project: str) -> str:
    """Validated projects/<slug>/memory path (traversal-guarded, must exist)."""
    return lib.safe_subdir(PROJECTS_DIR, project, "memory")


def _list() -> dict:
    projects = []
    if os.path.isdir(PROJECTS_DIR):
        changes = _memory_changes()
        for slug in sorted(os.listdir(PROJECTS_DIR)):
            mem_dir = os.path.join(PROJECTS_DIR, slug, "memory")
            if not os.path.isdir(mem_dir):
                continue
            files = [n for n in os.listdir(mem_dir) if n.endswith(".md")]
            if not files:
                continue
            # MEMORY.md first, then alphabetical
            files.sort(key=lambda n: (n != "MEMORY.md", n.lower()))
            projects.append({"project": slug, "files": files, "changes": changes.get(slug, [])})
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
