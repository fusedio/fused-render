"""Which canonical skills exist, and where each one's source is (D490).

Two things hand these skills to a Claude session off a list of names, and each
one used to carry its own copy of that list:

* the plugin root every session fused-render spawns is handed
  (``skill_plugin.py``, D216), and
* the packaged copy at ``fused_render/skills/`` that the root is assembled from
  on a machine with no repo (``scripts/hatch_build.py``, D106).

A third delivery never had a list at all: the repo root **is** a plugin root
(committed ``.claude-plugin/plugin.json`` beside ``skills/``), so the published
``fusedio/fused-render`` plugin — which is what covers sessions we did NOT spawn
(``user_plugin.py``, D492) — ships whatever is in ``skills/``. That asymmetry is
what makes a list wrong rather than merely tedious — the
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
packaged copy under ``fused_render/skills/`` (wheel builds). D106's rule is
about which skill EXISTS too, not only what is inside one it already knows
about: a repo checkout is truth about its own membership, so a skill deleted
or renamed out of ``skills/`` must stop shipping even when a stale
``fused_render/skills/`` (a leftover local wheel build) still has the old
directory sitting there. Unioning the two roots per-skill would let that
stale copy keep a dead skill alive forever on exactly the machine — a dev
checkout — where the repo answer is available and correct.

``scripts/hatch_build.py`` deliberately does NOT import this module — a build
hook must not import the package it is building — and carries the same scan
instead; ``tests/test_skill_plugin.py`` pins the two against each other on the
real repo, which is the guard the hardcoded lists never had.
"""
import os

# The two source roots, resolved once. They live HERE rather than once per
# consumer: they were duplicated across `skill_plugin` and the user-level skill
# copy (D185, since deleted) in different spellings, pinned equal by a test — a
# seam that only ever needed to be one thing.
REPO_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
PACKAGED_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "skills")

# What makes a directory a skill. Also what keeps the packaged tree's flat
# `plugin.json` (a file beside the skill dirs, see hatch_build) and any stray
# non-skill directory out of every delivery.
MANIFEST_FILE = "SKILL.md"


def skill_sources() -> dict:
    """``{name: source dir}`` for every canonical skill, repo root winning
    WHOLESALE over the packaged one (D106).

    Per ROOT, not per skill: a resolvable `skills/` is a dev/editable checkout,
    and that checkout is the current truth about its own membership, not just
    about the content of skills it already lists. So the packaged copy is
    consulted only when the repo root cannot be listed at all (a wheel/DMG
    install with no repo present). If it were unioned per-skill instead, a
    skill deleted or renamed out of `skills/` would keep shipping forever on a
    dev checkout that also has a stale `fused_render/skills/` copy lying
    around (a leftover local wheel build) — the one place where the correct
    answer (the repo) is sitting right there and gets overridden by a stale
    one anyway. `_source_for`, before this was a scan, could not exhibit that
    bug (it was handed one name at a time by a hardcoded list, never asked
    "which names exist"), so matching its old per-name fallback here would be
    reproducing a gap it never had to close.

    A missing repo root is not an error — a wheel install has no repo, so the
    packaged copy is the only source it could ever have.

    "Resolvable" means it yields at least one skill, not merely that it lists.
    REPO_SKILLS_DIR is `<parent of the package>/skills`, which on a wheel
    install is `<site-packages>/skills` — a path any other distribution is free
    to create. An empty or non-skill `skills/` there would otherwise be read as
    an authoritative "this checkout has no skills" and deliver NOTHING, on
    exactly the installs that have only the packaged copy to offer. That is the
    failure this whole PR exists to stop, so precedence is claimed by a root
    that actually has skills in it.
    """
    out = _scan(REPO_SKILLS_DIR)
    return out or _scan(PACKAGED_SKILLS_DIR)


def _scan(root: str) -> dict:
    """``{name: dir}`` for one root. Unreadable or absent reads as empty: the
    caller distinguishes the two roots, not the two failure modes."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return {}
    out = {}
    for name in names:
        src = os.path.join(root, name)
        if os.path.isfile(os.path.join(src, MANIFEST_FILE)):
            out[name] = src
    return out


def skill_names() -> tuple:
    """The canonical skill names, in the order `skill_sources` resolved them."""
    return tuple(skill_sources())
