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


def test_h3_commit_pin_is_single_sourced():
    """H3_COMMIT must come from scripts/h3_commit.txt, not a literal.

    scripts/dev.sh builds the same antirez/h3.c pin (for the h3-video runner in
    a dev checkout) and MUST read the identical commit — a second hardcoded sha
    is exactly the drift this repo has been bitten by elsewhere (see
    RCLONE_VERSION, duplicated across build_dmg.sh and
    build_linux_appimage.sh). scripts/h3_commit.txt is the one place a pin bump
    may be written.
    """
    src = _script()
    assert 'H3_COMMIT="$(tr -d \'[:space:]\' < "$REPO_ROOT/scripts/h3_commit.txt")"' in src, (
        "build_dmg.sh must read H3_COMMIT from scripts/h3_commit.txt, not a "
        "hardcoded sha, so a pin bump cannot drift out of sync with dev.sh"
    )
    # No leftover 40-hex-char literal assignment anywhere else in the script.
    assert not re.search(r'H3_COMMIT="[0-9a-f]{40}"', src), (
        "found a hardcoded H3_COMMIT sha literal — the pin must live only in "
        "scripts/h3_commit.txt"
    )

    commit_file = os.path.join(_SCRIPTS, "h3_commit.txt")
    with open(commit_file, encoding="utf-8") as f:
        pinned = f.read().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", pinned), (
        f"scripts/h3_commit.txt must hold exactly one 40-char hex sha, got: {pinned!r}"
    )

    dev_sh = os.path.join(_SCRIPTS, "dev.sh")
    with open(dev_sh, encoding="utf-8") as f:
        dev_src = f.read()
    assert "scripts/h3_commit.txt" in dev_src, (
        "dev.sh must read the same scripts/h3_commit.txt file build_dmg.sh reads"
    )
    assert not re.search(r'h3_commit="[0-9a-f]{40}"', dev_src), (
        "found a hardcoded h3 commit literal in dev.sh — it must read "
        "scripts/h3_commit.txt like build_dmg.sh does"
    )


def test_h3_license_is_staged_and_shipped():
    """h3.c is MIT: the notice must travel with every copy of the binary.

    A dev-only local build (dev.sh) is not redistribution, but this script
    ships the compiled h3 binary inside the DMG — that IS redistribution, so
    the upstream LICENSE has to be staged alongside the cached binary and
    copied into the app bundle next to it, the same as the binary itself.
    """
    src = _script()
    assert 'cp "$H3_SRC_DIR/LICENSE" "$H3_STAGE_DIR/LICENSE"' in src, (
        "the h3.c LICENSE must be staged into the build cache alongside the "
        "compiled binary"
    )
    assert 'cp "$H3_STAGED_LICENSE" "$APP_DIR/Contents/Resources/bin/h3-LICENSE"' in src, (
        "the staged h3.c LICENSE must be copied into the app bundle next to "
        "the h3 binary it accompanies"
    )


def test_dev_sh_h3_build_is_soft_fail_with_an_opt_out():
    """dev.sh's h3 build must never be able to abort the dev server start.

    Every failure mode (no Apple Silicon, no network, missing git/clang, a
    failed compile) has to warn and return 0 under dev.sh's own
    `set -euo pipefail` — and there must be an env var to skip the compile
    entirely, in the FUSED_RENDER_* naming convention this script already
    uses for its other knobs (FUSED_RENDER_NO_RELOAD, etc).
    """
    dev_sh = os.path.join(_SCRIPTS, "dev.sh")
    with open(dev_sh, encoding="utf-8") as f:
        src = f.read()
    assert "FUSED_RENDER_SKIP_H3_BUILD" in src, (
        "dev.sh needs an opt-out env var to skip the h3 compile"
    )
    assert "export FUSED_RENDER_H3_BIN=" in src, (
        "dev.sh must export FUSED_RENDER_H3_BIN so registry.h3_bin() (its "
        "first resolution step) finds the freshly-built/cached binary"
    )
    func_match = re.search(
        r"_maybe_build_h3\(\) \{(.*?)\n\}\n", src, re.DOTALL
    )
    assert func_match, "expected a _maybe_build_h3 function in dev.sh"
    body = func_match.group(1)
    # Every early-exit path must `return 0` (not a bare non-zero exit) so a
    # failure inside the function cannot itself register as the function's
    # failing exit status in a way that surprises the `|| …` guard at the
    # call site — and, more importantly, so it never calls a bare `exit`,
    # which WOULD take the whole of dev.sh down with it.
    assert "exit 1" not in body and re.search(r"\bexit\b", body) is None, (
        "_maybe_build_h3 must never call `exit` — only `return 0` on failure — "
        "or a failure would abort the whole dev server start"
    )
    assert body.count("return 0") >= 5, (
        "expected a `return 0` soft-fail on each of: opt-out, non-Apple-Silicon, "
        "missing git, missing clang/cc, clone failure, checkout failure, and "
        "compile failure"
    )
    # The call site must guard the whole function with `||`, which is what
    # suspends `set -e` for every command inside the function body.
    assert re.search(r"_maybe_build_h3 \|\|", src), (
        "the call to _maybe_build_h3 must be guarded with `||` so a failing "
        "command inside it cannot abort dev.sh under set -e"
    )
