"""Which canonical skills exist, and where each one's source is (D490).

Three deliveries hand the SAME skills to a Claude session, and each one used to
carry its own hardcoded list of names:

* the plugin root every session fused-render spawns is handed
  (``skill_plugin.py``, D216),
* the best-effort user-level sync for sessions we did NOT spawn
  (``user_skills.py``, D185),
* the packaged copy at ``fused_render/skills/`` that both of those read on a
  machine with no repo (``scripts/hatch_build.py``, D106).

A fourth delivery never had a list: the repo root **is** a plugin root
(committed ``.claude-plugin/plugin.json`` beside ``skills/``), so
``claude plugin marketplace add fusedio/fused-render`` ships whatever is in
``skills/``. That is what makes a list wrong rather than merely tedious — the
published plugin and our own deliveries could disagree about what a skill even
is, and they did: ``fused-render-ai`` was added to both runtime lists and not to
the build hook's, so every wheel and DMG since shipped four of the five skills
and the fifth was simply unknown to any session on a machine without the repo.
Nothing failed; the model just didn't know the AI bridge existed. The list was
the only thing that had to be maintained in step, so there is no list any more:
a skill IS a directory under ``skills/`` with a ``SKILL.md`` in it.

Nothing here is authoritative about the CONTENT of a skill — that is D106's
single-source rule, unchanged: the repo-level ``skills/<name>/`` wins whenever
it is resolvable (editable/dev installs — always the current truth), else the
packaged copy under ``fused_render/skills/`` (wheel builds).

``scripts/hatch_build.py`` deliberately does NOT import this module — a build
hook must not import the package it is building — and carries the same scan
instead; ``tests/test_skill_plugin.py`` pins the two against each other on the
real repo, which is the guard the three lists never had.
"""
import os

# The two source roots, resolved once. They live HERE rather than once per
# consumer: they were duplicated across `skill_plugin` and `user_skills` in
# different spellings, pinned equal by a test — a seam that only ever needed to
# be one thing.
REPO_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
PACKAGED_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "skills")

# What makes a directory a skill. Also what keeps the packaged tree's flat
# `plugin.json` (a file beside the skill dirs, see hatch_build) and any stray
# non-skill directory out of every delivery.
MANIFEST_FILE = "SKILL.md"


def skill_sources() -> dict:
    """``{name: source dir}`` for every canonical skill, repo root winning per
    skill (D106).

    Per SKILL, not per root: a dev checkout whose repo `skills/` is missing one
    that a stale local wheel-build copy still has should deliver both, the same
    way `_source_for` did before this was a scan.

    A missing root is not an error — a wheel install has no repo and an editable
    install has no packaged copy, so exactly one of the two is normally absent.
    """
    out = {}
    for root in (REPO_SKILLS_DIR, PACKAGED_SKILLS_DIR):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue  # root absent (or unreadable): the other one may have it
        for name in names:
            src = os.path.join(root, name)
            if name not in out and os.path.isfile(
                    os.path.join(src, MANIFEST_FILE)):
                out[name] = src
    return out


def skill_names() -> tuple:
    """The canonical skill names, in the order `skill_sources` resolved them."""
    return tuple(skill_sources())
