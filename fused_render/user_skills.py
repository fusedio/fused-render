"""Sync the canonical fused-render skills into Claude Code's **user-level**
skills directory (``<CLAUDE_CONFIG_DIR or ~/.claude>/skills/<name>/``), so any
Claude session — an app folder, a template folder, a plain terminal in the
workspace — can invoke them by name. This is the ONLY place skills are copied
to (D185): scaffolded app and template folders carry no ``.claude/`` of their
own any more.

Source resolution keeps D106's single-source rule: the repo-level
``skills/<name>/`` wins whenever it is resolvable (editable/dev installs —
always the current source of truth), else the packaged copy under
``fused_render/skills/`` (wheel builds; copied there at build time by
``scripts/hatch_build.py``, gitignored, shipped via pyproject's ``artifacts``
glob). If neither exists the skill is skipped — a missing skill must never
break server startup or scaffolding, so everything here is best-effort.

Ownership: a synced skill dir carries a marker file, and only marked dirs are
ever overwritten on re-sync. A same-named skill the user authored themselves
is left untouched. The sync target honours ``CLAUDE_CONFIG_DIR`` with the same
resolution the claude template backend uses (templates/claude/agent.py): the
desktop supervisor points it at the app's own state dir, so the skills land
exactly where the sessions we launch will look for them.
"""
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# All three canonical skills. Unlike the per-folder starter copies this
# replaces, the user-level install is machine-wide, so there is no reason to
# subset: usage guidance is as relevant as authoring.
SKILLS = (
    "fused-render-authoring",
    "fused-render-custom-templates",
    "fused-render-usage",
)

# Dropped into every dir we sync; its presence is what makes the dir ours to
# overwrite on the next sync (and its absence what protects a user-authored
# skill that happens to share the name).
_MARKER = ".managed-by-fused-render"

_REPO_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")
_PACKAGED_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


def claude_skills_dir() -> str:
    """Claude Code's user-level skills dir, honouring CLAUDE_CONFIG_DIR (the
    supervisor sets it for packaged builds — supervisor/paths.py)."""
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(claude_dir, "skills")


def _source_for(name: str) -> str | None:
    """Where to copy `name` from: live repo skills/ first (editable installs,
    and it also wins over a stale local wheel-build copy), else the packaged
    dir, else None."""
    for root in (_REPO_SKILLS_DIR, _PACKAGED_SKILLS_DIR):
        src = os.path.join(root, name)
        if os.path.isdir(src):
            return src
    return None


def sync_user_skills() -> None:
    """Install/refresh the canonical skills at the user level. Idempotent,
    best-effort per skill: refuses to touch an unmarked (user-owned) dir, and
    never raises — callers are server startup and app/template scaffolding,
    none of which may fail over a skill copy."""
    target_root = claude_skills_dir()
    for name in SKILLS:
        src = _source_for(name)
        if src is None:
            continue  # neither repo nor packaged source — nothing to sync
        target = os.path.join(target_root, name)
        try:
            if os.path.isdir(target) and not os.path.exists(
                os.path.join(target, _MARKER)
            ):
                continue  # user-authored skill with our name: theirs, not ours
            if os.path.lexists(target):
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
            with open(os.path.join(target, _MARKER), "w") as fh:
                fh.write(
                    "This skill is installed and kept up to date by fused-render.\n"
                    "Local edits will be overwritten on the next sync.\n"
                )
        except OSError as exc:
            logger.warning("could not sync skill %r to %s: %s", name, target, exc)
