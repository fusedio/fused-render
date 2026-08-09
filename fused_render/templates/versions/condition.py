"""Gate for the `versions` template — the HISTORY view.

Two rules, and the difference between them is the whole of this module:

* a **FILE** in a git work tree is offered its timeline. Any file, anywhere in
  any repository;
* a **FOLDER** is offered one when it is in a work tree AND it is a folder this
  app can RENDER — i.e. it has a top-level html page, by the shared entry rule
  (`shared/app_entry.entry_html`: `index.html`, else the first top-level
  `.html`). Every other folder is ignored here.

`main(path)` asks git itself the work-tree half — `rev-parse
--is-inside-work-tree`, from the directory when the target is one and from the
parent when it is a file (the probe `git/condition.py` documents at length).

**Why the html rule.** The gate briefly offered EVERY folder in a work tree, on
the argument that a folder has a timeline like anything else. True, and not the
question: this mode is a preview of the target *as it was*, and a folder with no
page renders as a file listing of a frozen tree — a thing worth having by URL
(the module still answers for one, below) and not worth a mode in the switcher
of every folder in every repository the user opens. The rule is deliberately the
SAME predicate the `app` view and the chat's pane resolve their page with, so
"this folder is something fused-render renders" has one answer across the app
rather than one per surface.

Two things this does NOT do, both deliberate:

* **It does not narrow the module.** `versions.py::_resolve_target` still
  answers for a plain folder, so a hand-written `?_mode=versions` URL on one
  works and shows its history. The gate decides what is OFFERED; the module
  decides what is ANSWERED, and the second is the guarantee (MD-11). It has to
  keep answering anyway: an older revision of an html-bearing folder may predate
  its html, and that commit's snapshot is a browsable tree.
* **It does not change write authority.** `revert` is still refused outside a
  fused app — that would write a commit with the Fused identity into the user's
  own repository (the rule `fused_render/linked_apps.py` sets for linked
  folders) — and auto-commits still happen only for real app folders. Being
  offered a timeline has never implied being given one.

**I/O discipline.** Mount-backed paths are refused OUTRIGHT and first, before
any stat, subprocess or listing: git over an rclone-NFS mount stats and lists
its way through the work tree, the exact pattern that wedges a flat million-key
S3 prefix. Everything after that point therefore runs on a LOCAL path only.

That refusal is what buys the one directory listing this gate does — the single
level `entry_html` reads, and the reason the peer gates' blanket "never
`os.listdir`" (app, claude, graph, zarr_aoi) does not transfer verbatim. Those
gates run on paths that may be remote, where a listing scales with entry count
and blows the mount's timeout; this one cannot. It is also not the expensive
part of this gate by any measure: a bounded `git` fork stands right beside it.
The ban that DOES still hold here is on walking — one level, never recursion.

Fails closed: any exception returns False.
"""


def main(path: str) -> bool:
    import os
    import sys

    try:
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on every stat.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False
        if not path:
            return False

        def _in_work_tree(cwd: str) -> bool:
            """`git rev-parse --is-inside-work-tree` asked from `cwd` — one
            bounded fork, never a search of the tree. The reasoning for asking
            git rather than stat'ing `.git` (a nested path has no `.git` of its
            own; a clone's `.git` is a dir and a worktree's is a file) lives in
            git/condition.py; this is the same probe, not a second copy of the
            rule."""
            import subprocess

            proc = subprocess.run(
                ["git", "--no-pager", "-C", cwd, "rev-parse",
                 "--is-inside-work-tree"],
                env={**os.environ,
                     "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                     "GIT_PAGER": "cat", "GIT_ASKPASS": ""},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=2.0,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
                close_fds=False,
            )
            return proc.returncode == 0 and proc.stdout.strip() == b"true"

        # `isdir` first and `isfile` second, deliberately never `not isdir`:
        # the loose form is also true for every path that does NOT EXIST, so a
        # missing name inside any repository would gate true. Same one-word trap
        # the peer gate documents (claude/condition.py), and "cannot tell" must
        # read as "refuse" (CT-12).
        if os.path.isdir(path):
            # A folder must be in a work tree AND renderable. Work tree first:
            # it is the cheaper of the two for the common negative (most folders
            # the explorer stats are not in a repository at all), and it is the
            # half that has to hold for BOTH shapes.
            if not _in_work_tree(path):
                return False
            # The page test, through the SHARED rule rather than a private
            # `.html` scan, so the gate and the view that opens cannot disagree
            # about what counts as renderable (`app_entry` is what `app/app.py`,
            # the chat's pane and `versions.py`'s own snapshot all resolve).
            # Local-only by construction — the mount refusal above already
            # returned.
            try:
                from app_entry import entry_html
            except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
                return False
            return entry_html(path) is not None
        if not os.path.isfile(path):
            return False
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            return False
        return _in_work_tree(parent)
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
