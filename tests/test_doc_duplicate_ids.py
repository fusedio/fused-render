"""SPEC.md/DECISIONS.md id collisions merge silently - catch them in CI instead.

Both files are hand-numbered (`## N. Title` sections in SPEC.md, `| DNNN | ... |`
decision rows in DECISIONS.md). Two branches independently adding the next
section or decision pick the same number, and a merge that touches disjoint
lines produces no conflict marker - the duplicate only surfaces if someone
happens to go looking, which a reviewer skimming a diff won't. This showed up
twice in practice: SPEC.md picked up a repeated section number across two
merges with no conflict either time, and DECISIONS.md still carries a couple
dozen D-numbers that were each claimed by two unrelated decisions.

SPEC.md's section numbers are checked with zero tolerance - there are none
today, so any new collision is new breakage. DECISIONS.md's pre-existing
collisions are too numerous and too heavily cross-cited (D72/D73 alone are
cited 30+ times each across SPEC.md/ARCHITECTURE.md) to safely renumber as a
side effect of adding this check, so they're grandfathered in
KNOWN_DUPLICATE_DECISION_IDS by their current count. Any count ABOVE that
baseline fails the build - a decision id can still only collide as much as it
already does, never more. Renumbering one away should lower (or drop) its
baseline entry; test_known_duplicate_decision_baseline_matches_reality catches
a baseline left stale after that cleanup.
"""
import re
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = _ROOT / "SPEC.md"
_DECISIONS = _ROOT / "DECISIONS.md"

_SECTION_HEADING_RE = re.compile(r"(?m)^## (\S+)\.\s")
_DECISION_ROW_RE = re.compile(r"(?m)^\| (D\d+) \|")

# DECISIONS.md ids already claimed by two (or, for D185, three) different
# decisions before this check existed. Keep this baseline in sync with reality:
# it should only ever shrink (a duplicate got renumbered away) or disappear
# (the last duplicate of that id was resolved) - never grow.
KNOWN_DUPLICATE_DECISION_IDS = {
    "D72": 2,
    "D73": 2,
    "D78": 2,
    "D97": 2,
    "D98": 2,
    "D99": 2,
    "D100": 2,
    "D105": 2,
    "D110": 2,
    "D114": 2,
    "D116": 2,
    "D120": 2,
    "D133": 2,
    "D153": 2,
    "D154": 2,
    "D155": 2,
    "D156": 2,
    "D157": 2,
    "D158": 2,
    "D159": 2,
    "D184": 2,
    "D185": 3,
}


def test_no_duplicate_spec_section_numbers():
    ids = _SECTION_HEADING_RE.findall(_SPEC.read_text(encoding="utf-8"))
    dupes = {sid: n for sid, n in Counter(ids).items() if n > 1}
    assert not dupes, (
        f"SPEC.md has '## N.' section headings reused by more than one section: "
        f"{dupes}. Two sections auto-merged onto the same number with no "
        "conflict marker to catch it - give the newer one the next unused "
        "number and fix any cross-references to it."
    )


def test_no_new_duplicate_decision_ids():
    ids = _DECISION_ROW_RE.findall(_DECISIONS.read_text(encoding="utf-8"))
    counts = Counter(ids)
    regressed = {
        did: n
        for did, n in counts.items()
        if n > KNOWN_DUPLICATE_DECISION_IDS.get(did, 1)
    }
    assert not regressed, (
        f"DECISIONS.md has decision id(s) claimed by more rows than the known "
        f"baseline allows: {regressed}. Two decisions auto-merged onto the same "
        "D-number with no conflict marker to catch it - give the newer one the "
        "next unused D-number and fix any cross-references to it."
    )


def test_known_duplicate_decision_baseline_matches_reality():
    ids = _DECISION_ROW_RE.findall(_DECISIONS.read_text(encoding="utf-8"))
    counts = Counter(ids)
    stale = {
        did: (baseline, counts.get(did, 0))
        for did, baseline in KNOWN_DUPLICATE_DECISION_IDS.items()
        if counts.get(did, 0) < baseline
    }
    assert not stale, (
        f"KNOWN_DUPLICATE_DECISION_IDS is stale (baseline -> actual): {stale}. "
        "Some of this id's duplication has been resolved - lower its baseline "
        "count here (or remove the entry if it's back to one row)."
    )
