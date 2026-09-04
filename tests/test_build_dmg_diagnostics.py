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

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")
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


# The macOS runner image WAS load-bearing (D468): `build_dmg.sh` bundled
# Homebrew's python@3.12 FRAMEWORK, and Homebrew ships prebuilt PER-OS bottles,
# so the image the DMG was built on decided which OS's system libraries the
# bundled interpreter's C extensions linked against. Building on macos-15
# shipped a `pyexpat.so` referencing `XML_SetReparseDeferralEnabled` -- a
# symbol macOS 14's /usr/lib/libexpat.1.dylib does not export -- so `import
# plistlib` failed at launch and py2app showed only its generic "Launch error"
# dialog. The apple tier (D700) then needed the macOS 26 SDK, so the pin moved
# from the RUNNER to the INTERPRETER: the build refuses Homebrew's python for a
# release, bundles the python.org-style framework `setup-python` installs, and
# step 4f fails the build on any Mach-O whose minos exceeds 14.0. These pins
# check the source for that arrangement, since the failure is silent on the
# machine that builds and only shows up on a user's older Mac.
_MACOS_WORKFLOW_JOBS = (
    ("release.yml", "build-sign-notarize-release"),
    ("test.yml", "macos-desktop"),
)


@pytest.mark.parametrize("workflow,job", _MACOS_WORKFLOW_JOBS)
def test_the_macos_build_runs_on_one_macos_26_job_with_a_portable_interpreter(workflow, job):
    path = os.path.join(_REPO, ".github", "workflows", workflow)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "runs-on: macos-26" in src, (
        f"{workflow}'s {job} job builds the apple tier helper in place and so "
        f"needs the macOS 26 SDK"
    )
    assert "apple-helper" not in src, (
        f"{workflow} grew a separate helper job again; the owner wants ONE mac "
        f"build job (the helper builds inside build_dmg.sh step 4e)"
    )
    assert "actions/setup-python" in src, (
        f"{workflow}'s {job} job must install setup-python's 3.12: on macOS "
        f"arm64 it is the python.org-style framework build_dmg.sh bundles"
    )


def test_build_dmg_refuses_a_homebrew_interpreter_for_a_release_and_guards_minos():
    src = _script()
    resolution = src.index('PORTABLE_FRAMEWORK_PYTHON="/Library/Frameworks/Python.framework')
    homebrew = src.index('HOMEBREW_FRAMEWORK_PYTHON="/opt/homebrew/opt/')
    assert resolution < homebrew, "the portable framework must be preferred over Homebrew's"
    assert 'FATAL: a release build needs a python.org-style framework python' in src
    assert 'MINOS_FLOOR="${FUSED_RENDER_MACOS_FLOOR:-14.0}"' in src, (
        "the step-4f minos guard is what lets the runner be newer than users' Macs"
    )
    assert 'otool -l' in src[src.index("# 4f."):src.index("# 5. Code signing")]
