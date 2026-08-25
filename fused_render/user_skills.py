"""Sync the canonical fused-render skills into Claude Code's **user-level**
skills directory (``<CLAUDE_CONFIG_DIR or ~/.claude>/skills/<name>/``), so any
Claude session — an app folder, a template folder, a plain terminal in the
workspace — can invoke them by name. Scaffolded app and template folders carry
no ``.claude/`` of their own any more (D185).

This sync is no longer load-bearing. It cannot be: writing into a dir we
*resolved* is a guess about the machine, and every way it can miss is silent
(see D216). The sessions fused-render itself spawns get the skills from
``skill_plugin.py``'s plugin root instead, passed explicitly as
``--plugin-dir``. What survives here is the case that mechanism cannot reach —
a session fused-render did NOT launch, e.g. the user's own ``claude`` in their
app folder — which is worth a best-effort copy and nothing more.

Which skills, and where each comes from, is ``skill_sources.py``'s answer
(D490) — a scan of ``skills/`` rather than a list to keep in step, keeping
D106's single-source rule: the repo-level ``skills/<name>/`` wins whenever it
is resolvable (editable/dev installs — always the current source of truth),
else the packaged copy under ``fused_render/skills/`` (wheel builds; copied
there at build time by ``scripts/hatch_build.py``, gitignored, shipped via
pyproject's ``artifacts`` glob). A skill with neither source simply isn't in
that mapping — a missing skill must never break server startup or scaffolding,
so everything here is best-effort.

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

from fused_render.skill_sources import skill_sources

logger = logging.getLogger(__name__)

# Dropped into every dir we sync; its presence is what makes the dir ours to
# overwrite on the next sync (and its absence what protects a user-authored
# skill that happens to share the name).
_MARKER = ".managed-by-fused-render"


def claude_skills_dir() -> str:
    """Claude Code's user-level skills dir, honouring CLAUDE_CONFIG_DIR (the
    supervisor sets it for packaged builds — supervisor/paths.py)."""
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(claude_dir, "skills")


def sync_user_skills() -> None:
    """Install/refresh the canonical skills at the user level. Idempotent,
    best-effort per skill: refuses to touch an unmarked (user-owned) dir, and
    never raises — callers are server startup and app/template scaffolding,
    none of which may fail over a skill copy.

    WHICH skills is not this module's decision (D490): every dir under
    `skills/` with a `SKILL.md` in it, resolved by `skill_sources`, so a skill
    added to the repo is delivered here without a list to remember. Unlike the
    per-folder starter copies this replaced, the user-level install is
    machine-wide, so there was never a reason to subset it either — usage
    guidance is as relevant as authoring."""
    target_root = claude_skills_dir()
    for name, src in skill_sources().items():
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
