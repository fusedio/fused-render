"""The design scale is the only source of type, spacing, radius and motion.

`frontend/src/styles/scale.css` declares the steps; hand-written CSS says
`var(--text-dense)` / `var(--space-3)` / `var(--radius-card)` /
`var(--dur-fast)`, and composites say `text-dense` / `p-3` / `rounded-card`.
A px literal for one of those properties anywhere else is a second source of
truth, and the two drift. This test refuses it.

Exceptions are real (a 14px SVG ring, a 1px optical nudge) and go in
`tests/design_scale_allowlist.txt` as `path::literal  # why`, one per line.
"""

from __future__ import annotations

import os
import re

import pytest

from _theme_sources import REPO_ROOT, read_repo_file  # noqa: F401

STYLES_DIR = os.path.join(REPO_ROOT, "frontend", "src", "styles")
SRC_DIR = os.path.join(REPO_ROOT, "frontend", "src")
ALLOWLIST_PATH = os.path.join(REPO_ROOT, "tests", "design_scale_allowlist.txt")

# Files that MAY carry literals: the scale itself, the shadcn bridge/theme
# mapping, and the vendored shadcn primitives (installed, not authored).
CSS_EXEMPT = {"scale.css", "tailwind.css"}
TSX_EXEMPT_DIRS = (os.path.join("platform", "shadcn") + os.sep,)

# Properties whose px values must come from the scale. `1px` is a hairline
# (borders, dividers) and stays a literal; `0` is not a size.
RHYTHM_PROPS = (
    "font-size",
    "border-radius",
    "border-top-left-radius",
    "border-top-right-radius",
    "border-bottom-left-radius",
    "border-bottom-right-radius",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "padding-inline",
    "padding-block",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "margin-inline",
    "margin-block",
    "gap",
    "row-gap",
    "column-gap",
)
DURATION_PROPS = ("transition", "transition-duration", "animation", "animation-duration")

_DECL = re.compile(r"(?P<prop>[a-z-]+)\s*:\s*(?P<value>[^;{}]+);", re.I)
_PX = re.compile(r"(?<![\w.-])(\d*\.?\d+)px\b")
_DUR = re.compile(r"(?<![\w.-])(\d*\.?\d+)(ms|s)\b")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# Tailwind arbitrary values that restate a rhythm literal.
_TW_ARBITRARY = re.compile(
    r"(?<![\w-])(?:-?)(?:text|leading|p|px|py|pt|pb|pl|pr|ps|pe|m|mx|my|mt|mb|ml|mr|"
    r"gap|gap-x|gap-y|space-x|space-y|inset|top|right|bottom|left|"
    r"rounded(?:-[trbl]{1,2}|-[se]{1,2}|-ss|-se|-ee|-es)?|duration|delay)"
    r"-\[(\d*\.?\d+)(px|ms|s)\]"
)


def _allowlist() -> set[tuple[str, str]]:
    if not os.path.exists(ALLOWLIST_PATH):
        return set()
    out: set[tuple[str, str]] = set()
    for raw in open(ALLOWLIST_PATH, encoding="utf-8"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        assert "::" in line, f"allowlist line needs path::literal — {raw!r}"
        path, literal = line.split("::", 1)
        out.add((path.strip(), literal.strip()))
    return out


def _css_files() -> list[str]:
    return sorted(
        n for n in os.listdir(STYLES_DIR) if n.endswith(".css") and n not in CSS_EXEMPT
    )


def _tsx_files() -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(SRC_DIR):
        rel_root = os.path.relpath(root, SRC_DIR)
        if any(rel_root.startswith(d.rstrip(os.sep)) for d in TSX_EXEMPT_DIRS):
            continue
        for f in files:
            if f.endswith((".tsx", ".ts")) and not f.endswith((".test.tsx", ".test.ts")):
                out.append(os.path.join(rel_root, f))
    return sorted(out)


def _css_offenders(name: str, source: str) -> list[str]:
    body = _COMMENT.sub("", source)
    found: list[str] = []
    for m in _DECL.finditer(body):
        prop = m.group("prop").lower()
        value = m.group("value")
        if prop in RHYTHM_PROPS:
            for px in _PX.findall(value):
                if px in ("0", "1", "0.5"):
                    continue
                found.append(f"{prop}: …{px}px")
        elif prop in DURATION_PROPS:
            for num, unit in _DUR.findall(value):
                if num in ("0", "0.01") or (unit == "ms" and num == "1"):
                    continue
                ms = float(num) * (1000 if unit == "s" else 1)
                if ms >= 500:
                    continue  # ambient loops (spinners, shimmer) are not UI motion
                found.append(f"{prop}: …{num}{unit}")
    return found


def _tsx_offenders(source: str) -> list[str]:
    return [f"{m.group(0)}" for m in _TW_ARBITRARY.finditer(source)]


@pytest.mark.parametrize("name", _css_files())
def test_stylesheet_takes_rhythm_from_the_scale(name):
    allow = _allowlist()
    source = read_repo_file(f"frontend/src/styles/{name}")
    offenders = [
        o for o in _css_offenders(name, source) if (f"styles/{name}", o) not in allow
    ]
    assert not offenders, (
        f"styles/{name}: px/duration literals for rhythm properties — use "
        f"var(--text-*), var(--space-*), var(--radius-*), var(--dur-*) from "
        f"scale.css, or allowlist with a reason: {sorted(set(offenders))}"
    )


def test_components_take_rhythm_from_the_scale():
    allow = _allowlist()
    bad: dict[str, list[str]] = {}
    for rel in _tsx_files():
        source = read_repo_file(f"frontend/src/{rel}")
        offenders = [o for o in _tsx_offenders(source) if (rel, o) not in allow]
        if offenders:
            bad[rel] = sorted(set(offenders))
    assert not bad, (
        "arbitrary rhythm values in components — use text-<role>, the numeric "
        "spacing scale, rounded-<name>, duration-(--dur-*), or allowlist with a "
        f"reason: {bad}"
    )


def test_scale_declares_every_role_tailwind_maps():
    scale = read_repo_file("frontend/src/styles/scale.css")
    tailwind = read_repo_file("frontend/src/styles/tailwind.css")
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+):", scale, re.M))
    mapped = set(re.findall(r"^\s*(--[a-z0-9-]+):\s*var\((--[a-z0-9-]+)\)", tailwind, re.M))
    for _name, target in mapped:
        if target.startswith(("--text-", "--radius-", "--font-", "--ease-glide")):
            assert target in declared, f"tailwind.css maps {target}, scale.css never declares it"


def test_allowlist_entries_are_still_needed():
    """An allowlist line whose literal no longer exists is debt; delete it."""
    for path, literal in sorted(_allowlist()):
        source = read_repo_file(f"frontend/src/{path}")
        if path.startswith("styles/"):
            present = literal in _css_offenders(os.path.basename(path), source)
        else:
            present = literal in _tsx_offenders(source)
        assert present, f"allowlist entry no longer matches anything: {path}::{literal}"
