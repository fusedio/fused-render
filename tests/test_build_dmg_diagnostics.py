"""Structural guards on scripts/build_dmg.sh's own failure reporting.

The DMG build cannot be exercised from CI on any other platform, and every step
guarded here exists PURELY to report a failure — so a bug in the reporting is
invisible by construction: the build still fails, just without the diagnostic
that says why. Same idiom as test_supervisor_linux_paths.py's payload-layout
guards, and for the same reason: the script is the single source of truth and
nothing else can check it.
"""
import os
import re

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
_DMG = os.path.join(_SCRIPTS, "build_dmg.sh")


def _script():
    with open(_DMG, encoding="utf-8") as f:
        return f.read()


def _capture_assignments(src):
    """(var, assignment text) for every `VAR="$(…)"`, joined across lines."""
    out = []
    lines = src.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r'\s*([A-Z_][A-Z0-9_]*)="\$\(', line)
        if not m:
            continue
        # The substitution can span lines (a `python -c` with a long snippet), so
        # take everything up to the line that closes it.
        chunk = [line]
        j = i
        while not chunk[-1].rstrip().endswith('"') or chunk[-1].rstrip() == line.rstrip() and not line.rstrip().endswith(')"'):
            j += 1
            if j >= len(lines) or j > i + 12:
                break
            chunk.append(lines[j])
            if chunk[-1].rstrip().endswith(')"'):
                break
        out.append((m.group(1), "\n".join(chunk)))
    return out


def test_a_captured_diagnostic_is_never_thrown_away_by_set_e():
    """`VAR="$(cmd 2>&1)"` under `set -euo pipefail` aborts on the ASSIGNMENT.

    Every smoke step in this script has the same shape: capture the output,
    `grep -q` it for the success marker, and on failure echo the capture to
    stderr. But when the command fails — the case the step exists for — the
    assignment fails, the ERR trap fires, and the script exits before the `if`
    and the `echo` ever run. The traceback that `2>&1` had just redirected INTO
    the variable is discarded, and the operator is told only "failed at line
    N". `|| true` inside the substitution (the idiom `UV_SRC` already uses)
    keeps the status out of the trap's way and lets the explicit check report.
    """
    src = _script()
    assert "set -euo pipefail" in src, (
        "this test's whole premise is `set -e`; if that went away, so did the bug"
    )
    unguarded = []
    for var, assignment in _capture_assignments(src):
        # Only the ones that DIAGNOSE: a variable whose value is echoed to stderr
        # in a failure branch is one whose capture must survive the failure.
        if f'echo "${var}" >&2' not in src:
            continue
        if "|| true" not in assignment:
            unguarded.append(var)
    assert unguarded == [], (
        "these captures are echoed in a FATAL branch that `set -e` can never "
        f"reach: {unguarded}"
    )


def test_the_force_list_reconciliation_does_not_repin_the_shipped_payload():
    """`$BUILD_VENV` IS the bundle's payload, so pip must not resolve into it.

    py2app runs under that venv and copies modules out of its site-packages, and
    the script `cp -R`s its purelib straight into the .app. Installing pytest
    there let pip pick versions of `pluggy`/`packaging`/`iniconfig` for PYTEST's
    constraints — pluggy is in setup_py2app.py's explicit force list and
    packaging reaches the bundle through the derivation closure, so a resolver
    bump lands in the shipped DMG with nothing but a pip warning. `--no-deps`
    keeps pip from touching anything the wheel's own resolution chose.

    The reconciliation itself is load-bearing (it is the only place [bundled] is
    genuinely installed, so it is the only place test_bundle_contents.py's
    per-distribution skips become assertions) and must stay.
    """
    src = _script()
    assert "tests/test_bundle_contents.py" in src, "the reconciliation must stay"
    # The rule is positional, because that is exactly where it bites: the
    # `[bundled,app,fused]` install IS the payload's dependency resolution and
    # must stay free to resolve. Everything after it inherits that answer, so
    # anything that could re-resolve it has to be pinned out with --no-deps.
    resolution = src.index('"$BUILD_VENV/bin/pip" install --quiet "${WHEEL_PATH}[bundled')
    tail = src[src.index("\n", resolution):]
    later = re.findall(r'"\$BUILD_VENV/bin/pip" install ([^\n]*)', tail)
    assert later, "the build venv installs disappeared; this test is stale"
    for flags in later:
        assert "--no-deps" in flags, (
            "this pip install runs AFTER the payload's dependency resolution and "
            "can silently change a version the DMG ships: "
            f'"$BUILD_VENV/bin/pip" install {flags}'
        )
