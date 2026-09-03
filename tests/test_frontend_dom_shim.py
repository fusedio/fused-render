"""The shared DOM shim's two process-wide rules (frontend/src/platform/lib/testDomShim.ts).

`bun test` runs every file in ONE process and does not reset globals between
them, and several frontend modules read `window`/`location`/`history` at MODULE
scope because a page load is genuinely when they need to know their environment
(router.ts rewrites a legacy path and computes IS_EMBED; appShot.ts registers a
pointerdown listener). Bun's runtime has no DOM, so something has to stand in.

Two things have to hold for that to be ORDER-INDEPENDENT, and neither is
visible in any single test file:

1. The shim is installed by `bunfig.toml`'s `[test] preload`, before the runner
   loads the first test file. That is the only hook early enough to cover a
   PLAIN STATIC import of such a module — a static import is hoisted above
   every statement in the importing file, so no in-file `installDomShim()` call
   can run first. Four files import router.ts exactly that way
   (shell/ActivityDock, platform/ui/StatusBar, platform/ui/DownloadManager,
   apps/ai_models/playground/appSeed) and each one fails on its own, with
   `ReferenceError: location is not defined` out of router.ts's module init,
   whenever nothing installed the globals ahead of it.

2. No suite DELETES one of those three globals in a teardown. A file's own
   "nothing below here needs it" is not the scope that matters: the delete
   lands on whatever module the NEXT file evaluates. Both shapes were measured
   to break the run — a delete at file top level and a delete inside a test
   body — so a suite that needs its own `window` (the listing's virtual
   `Clock`) or its own `history` (a nav-capturing `pushState`) saves the value
   it displaces and puts it back, via `restoreGlobal`.

Both rules are invisible to `tsc` and to any single-file run, and rule 1 only
FAILS once the file order shifts — which a merge that merely ADDS test files is
enough to do. Hence a test.
"""
import os
import re

import pytest

FRONTEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
SRC = os.path.join(FRONTEND, "src")
PRELOAD_REL = "./src/platform/lib/testDomShim.preload.ts"

# The globals the shim owns. `document` is deliberately NOT one of them:
# nothing reads it at module scope, the shim never installs it, and the one
# suite that installs its own removes it again — which restores the real
# initial state (absent) rather than stranding a later file.
OWNED = ("window", "location", "history")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code_lines(text, comment="//"):
    """The lines that are real code: a commented-out call is not a call.

    Learned by mutating this file's own subject: commenting out the preload's
    `installDomShim()` left the string `installDomShim()` sitting in the file,
    so a plain substring check went on passing against a preload that had
    become a no-op. Every scan below reads code, not prose.
    """
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped and not stripped.startswith(comment):
            yield line_no, line


def _ts_sources():
    for root, _dirs, files in os.walk(SRC):
        for name in files:
            if name.endswith((".ts", ".tsx")):
                path = os.path.join(root, name)
                yield os.path.relpath(path, FRONTEND).replace(os.sep, "/"), _read(path)


def test_bun_preloads_the_shared_dom_shim():
    """Rule 1: the shim runs before the runner loads the first test file."""
    bunfig = os.path.join(FRONTEND, "bunfig.toml")
    assert os.path.exists(bunfig), (
        "frontend/bunfig.toml is gone; without its [test] preload the four "
        "files that import router.ts statically pass only by file-order luck")
    code = [line for _no, line in _code_lines(_read(bunfig), comment="#")]
    assert any("[test]" in line for line in code), (
        "bunfig.toml has no live [test] section")
    assert any(PRELOAD_REL in line for line in code), (
        "bunfig.toml's [test] preload must list %s — that is what installs the "
        "DOM shim early enough for a static import" % PRELOAD_REL)


def test_the_preload_module_actually_installs_the_shim():
    """A preload entry that imports nothing useful is a silent no-op."""
    preload = os.path.join(SRC, "platform", "lib", "testDomShim.preload.ts")
    assert os.path.exists(preload), "the file bunfig.toml preloads does not exist"
    called = any(
        line.strip().startswith("installDomShim()")
        for _no, line in _code_lines(_read(preload)))
    assert called, (
        "the preload module must CALL installDomShim() as a live statement; "
        "importing it is not enough (bun evaluates a preload for its side "
        "effects) and neither is a commented-out call")


def test_the_shim_carries_every_member_the_suites_reach_for():
    """A half-missing `window` is worse than none: it is truthy."""
    shim = "\n".join(
        line for _no, line
        in _code_lines(_read(os.path.join(SRC, "platform", "lib", "testDomShim.ts"))))
    for member in ("dispatchEvent", "addEventListener", "removeEventListener",
                   "setTimeout", "clearTimeout", "setInterval", "clearInterval"):
        assert member in shim, (
            "the shared `window` stub dropped `%s`; whoever installs first wins "
            "for the whole process, so a member missing here surfaces as "
            "`window.%s is not a function` in a file that never touched it"
            % (member, member))
    for member in ("pathname", "search", "href", "origin"):
        assert member in shim, "the shared `location` stub dropped `%s`" % member


def test_no_suite_deletes_a_global_the_shim_owns():
    """Rule 2: a teardown puts back what it displaced; it never deletes."""
    # `delete globalThis.window`, and the cast forms the suite actually uses:
    # `delete (globalThis as Record<string, unknown>).window`.
    pattern = re.compile(
        r"delete\s+(?:globalThis|\(\s*globalThis\s+as[^)]*\))\.(%s)\b" % "|".join(OWNED))
    offenders = []
    for rel, text in _ts_sources():
        for line_no, line in _code_lines(text):
            hit = pattern.search(line)
            if hit:
                offenders.append("%s:%d deletes `%s`" % (rel, line_no, hit.group(1)))
    assert not offenders, (
        "these teardowns delete a global the shared DOM shim owns:\n  "
        + "\n  ".join(offenders)
        + "\n\nSave the value being displaced and restore it with "
          "`restoreGlobal` (platform/lib/testDomShim.ts) instead. A delete "
          "leaves the NEXT file to evaluate router.ts with no `location` at "
          "all, which fails in a file that never touched this suite.")


@pytest.mark.parametrize("rel", [
    "src/shell/ActivityDock.test.tsx",
    "src/platform/ui/StatusBar.test.tsx",
    "src/platform/ui/DownloadManager.test.tsx",
    "src/apps/ai_models/playground/appSeed.test.ts",
])
def test_the_statically_importing_suites_are_the_ones_the_preload_covers(rel):
    """Why rule 1 is not redundant with an in-file `installDomShim()` call.

    These four reach router.ts through a hoisted STATIC import, so they carry
    no shim of their own and cannot: the import is evaluated before any
    statement in the file. They are named here so that a rewrite which gives
    one of them a dynamic import — or which adds a fifth file in this shape —
    has to come past this list and the reasoning above it.
    """
    path = os.path.join(FRONTEND, rel)
    assert os.path.exists(path), (
        "%s moved; re-check whether it still imports router.ts statically and "
        "update this list either way" % rel)
    text = "\n".join(line for _no, line in _code_lines(_read(path)))
    assert "installDomShim" not in text, (
        "%s now calls installDomShim() — if it also switched to `await "
        "import()`, drop it from this list; if it did not, the call is dead "
        "code that reads as protection it cannot give" % rel)
