#!/usr/bin/env bash
# Build FusedRender.app + a distributable DMG via py2app (SPEC §12, D33-D35).
#
# Pipeline: pick/bootstrap a FRAMEWORK-build python (py2app needs one to
# produce a real standalone bundle, see the note below) -> build the wheel
# once (dist/*.whl) -> build a venv on it -> pip install that wheel
# [bundled,app] + py2app + dmgbuild into the venv -> generate the app icon ->
# run py2app -> codesign -> dmgbuild -> notarize.
#
# Signing (step 5, D73) is credential-driven: with a Developer ID identity in
# your keychain the bundle is signed with the hardened runtime (and optionally
# notarized + stapled); with no identity it falls back to the old ad-hoc sign
# so a plain `bash scripts/build_dmg.sh` still works for local testing. See
# docs/signing.md for how to get an identity and store notary credentials.
#
# This replaces the earlier hand-rolled tarball-shim assembly (D29-D32): that
# approach's bare bash-shim launch was the likely cause of flaky
# NSStatusItem/AppKit behavior under Finder launches. py2app's compiled stub
# executable gives the process proper LaunchServices/AppKit identity, and
# modern py2app still ships a REAL python interpreter in the bundle (not a
# PyInstaller-style freeze) — `sys.executable` inside the running app is a
# real, re-invokable interpreter, so the subprocess executor
# (executor.py/_child.py) keeps working completely unchanged (D33).
set -euo pipefail

# Every failure reports itself. `set -e` aborts with NO message when the failing
# command is quiet — a bare `test -d`, a subshell whose child already printed
# something that looked like success, or a process killed by a signal. That is
# exactly how a CI run of this script died between py2app and the next step with
# no error at all: the only clue was which `==>` heading had not printed yet.
# One trap turns any such exit into a located failure, for this bug and the next.
_build_failed() {
  local status=$?
  echo "" >&2
  echo "FATAL: build_dmg.sh failed at line ${BASH_LINENO[0]:-?} (exit $status)" >&2
  echo "       command: ${BASH_COMMAND}" >&2
  if [[ $status -gt 128 ]]; then
    echo "       exit > 128 means KILLED BY SIGNAL $((status - 128)) — most likely" >&2
    echo "       the OS reclaiming memory, not a bug in the command itself." >&2
  fi
  echo "       disk:" >&2
  df -h "${BUILD_DIR:-$PWD}" >&2 2>/dev/null || true
  exit "$status"
}
trap _build_failed ERR

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$(PYTHONPATH="$REPO_ROOT" python3 -m fused_render._branch ref)"
SUFFIX="$(PYTHONPATH="$REPO_ROOT" python3 -m fused_render._branch suffix)"
APP_NAME="FusedRender${SUFFIX}"

# Single source of truth is fused_render/__init__.py's __version__ (pyproject
# derives it dynamically via [tool.hatch.version], so it has no literal version
# line to grep). Regex the file rather than importing the package — no import
# side effects, and it works before deps are installed.
VERSION="$(python3 -c "
import re
text = open('${REPO_ROOT}/fused_render/__init__.py').read()
print(re.search(r'(?m)^__version__\s*=\s*\"([^\"]+)\"', text).group(1))
")"

BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"
BUILD_VENV="$BUILD_DIR/py2app-venv"
PY2APP_DIST="$BUILD_DIR/py2app-dist"
ICNS_PATH="$BUILD_DIR/${APP_NAME}.icns"
APP_DIR="$PY2APP_DIST/${APP_NAME}.app"
DMG_PATH="$DIST_DIR/${APP_NAME}-${VERSION}.dmg"

echo "==> fused-render ${VERSION} -> ${APP_NAME}.app (py2app) -> ${DMG_PATH##*/}"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# ---------------------------------------------------------------------------
# 1. Pick a FRAMEWORK-build python for the build venv.
#
#    py2app assembles a standalone bundle by copying the interpreter's own
#    Python.framework tree into Contents/Frameworks/ and pointing the app's
#    stub launcher at it; a non-framework build (e.g. python-build-standalone
#    "install_only" releases, or a plain --enable-shared/static Unix build)
#    doesn't have that framework layout for py2app to copy, and standalone
#    (non-"alias") builds on such interpreters are unreliable-to-broken.
#
#    Investigated on this machine:
#      - ~/.local/bin/python3.12 (used by the old pipeline): PYTHONFRAMEWORK
#        is empty -> NOT a framework build. Unusable for py2app standalone.
#      - /usr/bin/python3 (Apple's system python, 3.9): IS a framework build,
#        but 3.9 is below our >=3.10 floor and Apple could remove/relocate it
#        under SIP in future OS updates - not something to build a pipeline
#        around.
#      - Homebrew's `python@3.12` formula: also a genuine framework build
#        (Homebrew compiles CPython with --enable-framework on macOS) at
#        /opt/homebrew/opt/python@3.12/Frameworks/Python.framework/..., and
#        Homebrew itself needs no sudo on this machine (/opt/homebrew is
#        user-owned). This is the one we use: pinned minor version (3.12,
#        matching the rest of the [bundled] stack's wheel availability),
#        no manual/relocatable-framework download needed, and — the actual
#        ask here — it's a one-command bootstrap (`brew install python@3.12`)
#        on a machine that doesn't have it yet, verified below by actually
#        building and running the app on it.
# ---------------------------------------------------------------------------

FRAMEWORK_FORMULA="python@3.12"
FRAMEWORK_PYTHON="/opt/homebrew/opt/${FRAMEWORK_FORMULA}/bin/python3.12"

if [[ ! -x "$FRAMEWORK_PYTHON" ]]; then
  echo "==> $FRAMEWORK_FORMULA not found, installing via Homebrew (no sudo needed)"
  brew install "$FRAMEWORK_FORMULA"
fi

FRAMEWORK_TAG="$("$FRAMEWORK_PYTHON" -c "import sysconfig; print(sysconfig.get_config_var('PYTHONFRAMEWORK') or '')")"
if [[ -z "$FRAMEWORK_TAG" ]]; then
  echo "FATAL: $FRAMEWORK_PYTHON is not a framework build (PYTHONFRAMEWORK is empty)." >&2
  echo "       py2app needs a framework python to produce a standalone .app; see the comment above this check." >&2
  exit 1
fi
echo "==> using framework python: $FRAMEWORK_PYTHON (PYTHONFRAMEWORK=$FRAMEWORK_TAG)"

# ---------------------------------------------------------------------------
# 2. Build the wheel (dist/*.whl), then a venv: that wheel[bundled,app,fused]
#    + py2app + dmgbuild.
#
#    The wheel is built ONCE here and reused for both installs below, rather
#    than each `pip install "${REPO_ROOT}[...]"` re-triggering
#    scripts/hatch_build.py's `npm install && npm run build` frontend step
#    from source. Installing an already-built .whl is a plain unpack — no
#    build hook runs — so this also gives the release pipeline a real
#    dist/*.whl artifact to upload/attach, instead of one pip builds
#    internally and discards after install.
# ---------------------------------------------------------------------------

export FUSED_RENDER_BRANCH="$REF"

if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
  echo "==> creating build venv"
  "$FRAMEWORK_PYTHON" -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip

echo "==> building wheel"
# Build via the venv's pip/python, not host python3 -m pip: on Homebrew python
# (and most other PEP 668 "externally managed" installs) a bare host-level
# `pip install` refuses to run, which would break the documented plain
# `bash scripts/build_dmg.sh` local path. A venv is always installable into.
# Clear stale dist/*.whl first so the glob below can't pick up a leftover
# wheel from an earlier version/run and resolve to more than one path.
rm -f "$DIST_DIR"/*.whl
"$BUILD_VENV/bin/pip" install --quiet --upgrade build
"$BUILD_VENV/bin/python" -m build --quiet --wheel --outdir "$DIST_DIR" "$REPO_ROOT"
WHEEL_PATH="$(ls "$DIST_DIR"/*.whl)"

echo "==> installing ${WHEEL_PATH##*/} [bundled,app,fused] + py2app + dmgbuild into the build venv"
# The `fused` engine/deploy CLI is baked into the bundle (SPEC §19 DP-3): the
# .app has no pip and no console scripts, so the Deploy surface runs the package
# in-interpreter via fused_render/_fused_cli.py under the bundled python.
# It now arrives through [bundled] — that is what setup_py2app.py's force-list
# DERIVES from, so [bundled] is what decides whether the DMG carries it (and
# tests/test_bundle_contents.py fails if it would not). Naming [fused] here as
# well is redundant, and kept deliberately: the extras list should still say out
# loud that this build wants the engine.
"$BUILD_VENV/bin/pip" install --quiet "${WHEEL_PATH}[bundled,app,fused]" py2app dmgbuild
# Force a fresh reinstall of fused-render itself every run so the branch ref
# baked into $WHEEL_PATH is picked up. The build venv is reused across builds,
# so pip would otherwise treat an unchanged version as already-satisfied and
# keep a stale _baked_branch.py from a previous ref/wheel.
"$BUILD_VENV/bin/pip" install --quiet --force-reinstall --no-deps --no-cache-dir "${WHEEL_PATH}"

# ---------------------------------------------------------------------------
# 2a-bis. Reconcile the force-list against what [bundled] actually installed.
#
# tests/test_bundle_contents.py answers "will the .app contain distribution X?"
# by asking setup_py2app.py's derivation and then checking X is reachable in the
# environment. Every one of its per-distribution checks SKIPS when X is not
# installed where pytest runs — which is why it is nearly toothless in the
# ordinary CI job, whose `pip install -e ".[dev]"` carries none of [bundled].
#
# This build venv is the one place in the whole pipeline where [bundled] is
# genuinely installed, so this is where those skips turn into assertions. Run it
# HERE — right after the install, before py2app spends minutes copying — so a
# distribution that would be silently absent from the bundle fails the build
# rather than shipping. This costs one pytest install; the [bundled] install it
# needs has already happened. It makes the check load-bearing on every path that
# builds a DMG (test.yml, release.yml, and a plain local `bash
# scripts/build_dmg.sh`) instead of only where someone remembered to wire it.
#
# FUSED_RENDER_REQUIRE_BUNDLED=1 makes the test's own per-distribution "not
# installed here, nothing to say" skips into failures, so this cannot degrade
# into a green no-op if the install above ever stops carrying [bundled].
# `-o addopts=` clears the repo's `-n auto`, so this needs pytest but not xdist.
echo "==> reconciling the bundle force-list against the installed [bundled]"
# --no-deps, and it is load-bearing: this venv IS the payload. py2app runs under
# it and copies modules out of its site-packages, and its purelib is cp -R'd
# straight into the .app below. Left to resolve, pip picks pluggy/packaging/
# iniconfig for PYTEST's constraints — pluggy is in setup_py2app.py's explicit
# force list and packaging reaches the bundle through the derivation closure — so
# a version the DMG ships changes on a pip warning nobody reads. `iniconfig` is
# named because pytest needs it and nothing else here pulls it in; pluggy and
# packaging arrive with the wheel's own resolution above.
"$BUILD_VENV/bin/pip" install --quiet --no-deps pytest iniconfig
# Which means a missing pytest dependency now fails as an ImportError instead of
# being quietly installed over the payload's pin. Said out loud, with the fix, so
# it cannot read as a broken reconciliation step.
if ! "$BUILD_VENV/bin/python" -c 'import pytest' >/dev/null 2>&1; then
  echo "FATAL: pytest does not import in the build venv." >&2
  echo "       It is installed --no-deps on purpose (this venv is the shipped" >&2
  echo "       payload), so something pytest needs is not among the wheel's own" >&2
  echo "       dependencies any more. Add it to the --no-deps install above." >&2
  "$BUILD_VENV/bin/python" -c 'import pytest' >&2 || true
  exit 1
fi
FUSED_RENDER_REQUIRE_BUNDLED=1 \
  "$BUILD_VENV/bin/python" -m pytest -q -o addopts= \
  "$REPO_ROOT/tests/test_bundle_contents.py"

# ---------------------------------------------------------------------------
# 2b. Stage rclone (D103): mounts (shell/mounts.py, D102) shell out to a real
#     rclone binary. Bundling it means mounts work with zero user setup - no
#     brew/apt install, no PATH dependency - instead of the old "one
#     prerequisite: rclone" ask (README). Pinned version + published sha256
#     (arm64 only, matching this script's Apple Silicon-only py2app target -
#     see the FRAMEWORK_PYTHON note above); bump both together to upgrade.
#     Cached under build/ keyed by version, so re-running the script doesn't
#     re-download unless the pin changes.
# ---------------------------------------------------------------------------

RCLONE_VERSION="v1.74.4"
RCLONE_ZIP="rclone-${RCLONE_VERSION}-osx-arm64.zip"
RCLONE_SHA256="c2100e2d4a4b3be04c55cd45380cafe7647e1ad772bb055f52f00876ed701167"
RCLONE_STAGE_DIR="$BUILD_DIR/rclone-bin/${RCLONE_VERSION}"
RCLONE_STAGED_BIN="$RCLONE_STAGE_DIR/rclone"

if [[ ! -x "$RCLONE_STAGED_BIN" ]]; then
  echo "==> downloading rclone ${RCLONE_VERSION} (osx-arm64)"
  RCLONE_DL_DIR="$BUILD_DIR/rclone-download"
  rm -rf "$RCLONE_DL_DIR"
  mkdir -p "$RCLONE_DL_DIR"
  curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/${RCLONE_ZIP}" \
    -o "$RCLONE_DL_DIR/$RCLONE_ZIP"

  ACTUAL_SHA256="$(shasum -a 256 "$RCLONE_DL_DIR/$RCLONE_ZIP" | cut -d' ' -f1)"
  if [[ "$ACTUAL_SHA256" != "$RCLONE_SHA256" ]]; then
    echo "FATAL: rclone download checksum mismatch." >&2
    echo "       expected: $RCLONE_SHA256" >&2
    echo "       actual:   $ACTUAL_SHA256" >&2
    exit 1
  fi

  (cd "$RCLONE_DL_DIR" && unzip -q "$RCLONE_ZIP")
  mkdir -p "$RCLONE_STAGE_DIR"
  cp "$RCLONE_DL_DIR/rclone-${RCLONE_VERSION}-osx-arm64/rclone" "$RCLONE_STAGED_BIN"
  chmod +x "$RCLONE_STAGED_BIN"
  rm -rf "$RCLONE_DL_DIR"
else
  echo "==> rclone ${RCLONE_VERSION} already staged, skipping download"
fi

# ---------------------------------------------------------------------------
# 3. App icon: a fresh, high-res render of the same four-pointed sparkle used
#    for the menu-bar glyph (fused_render/assets/menubar-template.png, 36px,
#    template/monochrome) on a rounded dark card, at the sizes iconutil wants.
#    Build artifact only - never committed (BUILD_DIR is gitignored).
# ---------------------------------------------------------------------------

echo "==> generating app icon"
ICONSET_DIR="$BUILD_DIR/${APP_NAME}.iconset"
rm -rf "$ICONSET_DIR" "$ICNS_PATH"
mkdir -p "$ICONSET_DIR"

"$BUILD_VENV/bin/python" - "$ICONSET_DIR" <<'PYEOF'
import math
import sys
from PIL import Image, ImageDraw

iconset_dir = sys.argv[1]
SUPERSAMPLE = 4
CANVAS = 1024 * SUPERSAMPLE

# Dark rounded card, matching the shell's dark theme (--bg-alt).
bg = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
draw = ImageDraw.Draw(bg)
margin = CANVAS * 0.06
radius = CANVAS * 0.22
draw.rounded_rectangle(
    [margin, margin, CANVAS - margin, CANVAS - margin],
    radius=radius,
    fill=(27, 29, 33, 255),  # --bg-alt
)

# Four-pointed sparkle: 4 outer tips (N/E/S/W) joined by quadratic-Bezier
# concave arcs whose control point sits close to the center, echoing the
# menu-bar template glyph's shape at icon resolution.
cx = cy = CANVAS / 2
outer_r = CANVAS * 0.34
waist_r = CANVAS * 0.09
tips = [(-90, outer_r), (0, outer_r), (90, outer_r), (180, outer_r)]
waists = [(-45, waist_r), (45, waist_r), (135, waist_r), (-135, waist_r)]


def point(angle_deg, r):
    a = math.radians(angle_deg)
    return (cx + r * math.cos(a), cy + r * math.sin(a))


poly = []
for i in range(4):
    tip = point(*tips[i])
    nxt = point(*tips[(i + 1) % 4])
    ctrl = point(*waists[i])
    poly.append(tip)
    for t in [x / 12 for x in range(1, 12)]:
        x = (1 - t) ** 2 * tip[0] + 2 * (1 - t) * t * ctrl[0] + t**2 * nxt[0]
        y = (1 - t) ** 2 * tip[1] + 2 * (1 - t) * t * ctrl[1] + t**2 * nxt[1]
        poly.append((x, y))

draw.polygon(poly, fill=(229, 255, 68, 255))  # Fused yellow #E5FF44

sizes = [16, 32, 128, 256, 512]
for size in sizes:
    img = bg.resize((size, size), Image.LANCZOS)
    img.save(f"{iconset_dir}/icon_{size}x{size}.png")
    img2x = bg.resize((size * 2, size * 2), Image.LANCZOS)
    img2x.save(f"{iconset_dir}/icon_{size}x{size}@2x.png")
PYEOF

iconutil -c icns "$ICONSET_DIR" -o "$ICNS_PATH"
test -f "$ICNS_PATH"

# ---------------------------------------------------------------------------
# 4. Run py2app
# ---------------------------------------------------------------------------

echo "==> running py2app"
rm -rf "$PY2APP_DIST" "$BUILD_DIR/py2app-build"
# Run from BUILD_DIR, not REPO_ROOT: setuptools auto-discovers a
# pyproject.toml in the current working directory and tries to merge its
# [project] metadata into this ad-hoc setup() call, which errors ("install_
# requires is no longer supported") against our real PEP 621 project file.
# setup_py2app.py resolves REPO_ROOT itself, so cwd doesn't affect what gets
# built - it just needs to not be a directory with its own pyproject.toml.
# `|| PY2APP_STATUS=$?` rather than letting `set -e` abort inside the subshell:
# py2app's last output is its own codesign success, so an abort here reads as
# "the build stopped for no reason" — which is precisely what it did in CI.
PY2APP_STATUS=0
(
  cd "$BUILD_DIR"
  FUSED_RENDER_ICNS="$ICNS_PATH" "$BUILD_VENV/bin/python" "$REPO_ROOT/scripts/setup_py2app.py" py2app \
    --dist-dir "$PY2APP_DIST" \
    --bdist-base "$BUILD_DIR/py2app-build"
) || PY2APP_STATUS=$?

if [[ $PY2APP_STATUS -ne 0 ]]; then
  echo "FATAL: py2app exited $PY2APP_STATUS" >&2
  if [[ $PY2APP_STATUS -gt 128 ]]; then
    echo "       (killed by signal $((PY2APP_STATUS - 128)) — py2app can print a" >&2
    echo "        successful-looking codesign line and then be killed; check memory" >&2
    echo "        and disk below rather than reading the last line as success)" >&2
  fi
  df -h "$BUILD_DIR" >&2 2>/dev/null || true
  exit "$PY2APP_STATUS"
fi
echo "==> py2app finished (exit 0)"
df -h "$BUILD_DIR" | tail -1

if [[ ! -d "$APP_DIR" ]]; then
  echo "FATAL: py2app reported success but $APP_DIR does not exist." >&2
  echo "       py2app-dist contains:" >&2
  ls -la "$PY2APP_DIST" >&2 2>/dev/null || echo "       (no $PY2APP_DIST at all)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4a. Prune dead weight from the bundle (D116). py2app's `packages` option
#     (setup_py2app.py) whole-copies each package, which drags along package
#     test suites (~100 MB: numpy/pandas/scipy/pyarrow/... ship `tests`
#     directories full of fixtures), stale `__pycache__` trees (~160 MB —
#     the .pyc files double every .py, and the bundle recompiles on first
#     run anyway), and the copied Python.framework's developer-only stdlib
#     corners (test suite, idlelib, ensurepip, lib2to3, tkinter — the GUI is
#     rumps/pyobjc, nothing imports tkinter) plus C headers. None of this is
#     importable by the app or by user scripts in any supported path; grep
#     of fused_render confirms nothing imports from a `tests` package.
#
#     MUST run before the smoke tests below (they validate the PRUNED
#     bundle) and before codesign (step 5 — deleting files after signing
#     would break the bundle seal).
# ---------------------------------------------------------------------------

echo "==> pruning bundle dead weight"
PRUNE_PYLIB="$APP_DIR/Contents/Resources/lib/python3.12"
PRUNE_FRAMEWORK="$APP_DIR/Contents/Frameworks/Python.framework"

# Package test suites: only directories literally named `tests` or `test`.
find "$PRUNE_PYLIB" -type d \( -name tests -o -name test \) -prune \
  -exec rm -rf {} +
# Stale bytecode caches (regenerated lazily at runtime in the user's
# __pycache__-less bundle are simply skipped — python falls back to source).
find "$APP_DIR/Contents/Resources/lib" -type d -name __pycache__ -prune \
  -exec rm -rf {} +
# Installer/dev tooling the app never runs: no pip in the bundle by design
# (SPEC §19 DP-3 — the Deploy surface uses the baked-in CLI, not pip).
#
# `_distutils_hack` + `distutils-precedence.pth` are setuptools' distutils shim,
# installed as top-level names BESIDE the `setuptools/` package rather than
# inside it — so deleting setuptools alone left an orphaned hack with nothing
# behind it, and that orphan was not inert. Any python started with this bundle
# on its path (uv's build-isolation interpreters, before D266 taught
# `_env_install_worker` to scrub PYTHONHOME) imported the app's frozen copy in
# place of the one shipped with the setuptools actually doing the build, and
# every source build died on `No module named 'jaraco.text'`. The env scrub is
# the fix; this is the other half — a shim for a package that is not here has no
# reason to ship.
rm -rf "$PRUNE_PYLIB/pip" "$PRUNE_PYLIB/setuptools" "$PRUNE_PYLIB/wheel" \
       "$PRUNE_PYLIB/pkg_resources" "$PRUNE_PYLIB/PyObjCTest" \
       "$PRUNE_PYLIB/_distutils_hack" "$PRUNE_PYLIB/distutils-precedence.pth"
# The copied Python.framework: stdlib test suite + developer-only modules +
# C headers. The app's own stdlib lives here (Resources/lib holds packages),
# so prune surgically, never wholesale.
FW_LIB="$PRUNE_FRAMEWORK/Versions/3.12/lib/python3.12"
rm -rf "$FW_LIB/test" "$FW_LIB/idlelib" "$FW_LIB/ensurepip" \
       "$FW_LIB/lib2to3" "$FW_LIB/tkinter" \
       "$FW_LIB/site-packages/pip" "$FW_LIB/site-packages/setuptools" \
       "$FW_LIB/site-packages/wheel" \
       "$FW_LIB/site-packages/_distutils_hack" \
       "$FW_LIB/site-packages/distutils-precedence.pth"
rm -rf "$PRUNE_FRAMEWORK/Versions/3.12/include" \
       "$PRUNE_FRAMEWORK/Versions/3.12/Headers" \
       "$PRUNE_FRAMEWORK/Versions/3.12/share" \
       "$PRUNE_FRAMEWORK/Headers"
find "$PRUNE_FRAMEWORK" -type d -name __pycache__ -prune -exec rm -rf {} +

# Strip debug + local symbols from every bundled native library (D118).
# Wheels ship unstripped dylibs (pxr's libusd_ms alone carries ~96k symbols);
# -S drops debug info, -x drops local symbols, globals stay so dlopen/linking
# is untouched. Per-file `|| true`: find matches by extension, and a stray
# non-Mach-O .so (text stub, universal file strip dislikes) must not fail the
# build. MUST run before codesign (step 5) — stripping re-writes the file and
# would invalidate an existing signature.
echo "==> stripping debug symbols from bundled dylibs"
find "$APP_DIR" -type f \( -name '*.so' -o -name '*.dylib' \) \
  -exec sh -c 'for f do strip -S -x "$f" 2>/dev/null || true; done' _ {} +
echo "    pruned; app now $(du -sh "$APP_DIR" | cut -f1)"

# ---------------------------------------------------------------------------
# 4a-ter. Make the bundled interpreter SELF-LOCATING: Contents/lib -> Resources/lib.
#
#     py2app puts the runtime (stdlib zip + `lib/python3.12/` with the packages
#     and `config-3.12-darwin`) under `Contents/Resources/lib`, but the
#     interpreter itself at `Contents/MacOS/python`. CPython's landmark search
#     for `sys.prefix` walks up from the executable looking for
#     `<prefix>/lib/python3.12/...` — i.e. it checks `Contents/lib/python3.12`,
#     which does not exist — misses, and falls back to the prefix compiled into
#     the binary at build time: the BUILD MACHINE's Homebrew framework
#     (`/opt/homebrew/opt/python@3.12/...`), a path no user has. Measured on an
#     installed DMG, with the env stripped: `sys.prefix` was exactly that
#     Homebrew path. One relative symlink makes the landmark resolve inside the
#     .app and the interpreter fully self-locating with NO environment variables
#     at all (verified: `sys.prefix` = `<App>/Contents`, and `import
#     fused_render, duckdb, rasterio` all work).
#
#     The app itself worked WITHOUT this, because py2app's launcher stub exports
#     `PYTHONHOME=.../Contents/Resources` for the app process. What did not work
#     is everything downstream of that: `fused`'s `python_compute` STRIPS
#     PYTHONHOME/PYTHONPATH/VIRTUAL_ENV/PYTHONSTARTUP from its children, so a
#     `uv venv --python Contents/MacOS/python` (which is how the install loader
#     builds a venv for a PEP 723 header — SPEC PY-18, D176) recorded that
#     nonexistent Homebrew prefix as its base, and every child of that venv died
#     with `ModuleNotFoundError: No module named 'pandas'` / an encodings failure.
#     DMG users hit permanent "python execution" failures for exactly the pages
#     whose .py carries a header, with switching the engine pref to `local` as
#     the only workaround. So this symlink is what makes PEP 723 venvs built from
#     the bundled interpreter usable at all; it is not a tidiness fix.
#
#     Zero runtime cost: no probe, no wrapper, no extra subprocess — the symlink
#     is resolved by the interpreter's own startup path search.
#
#     Placement, exactly as it stands: AFTER the pruning in 4a, and BEFORE the
#     smoke tests and the codesign sweep (step 5) so the link is sealed into the
#     signature rather than added to a signed bundle. Note that package staging
#     (4a-bis) still runs AFTER this step despite coming later in the numbering —
#     that is fine and is the whole reason to be precise here: every one of those
#     steps writes INSIDE `Contents/Resources/lib/python3.12`, and this link lives
#     one level up at `Contents/lib`, so none of them can clobber it whichever
#     order they run in. What would break the link is a step that replaced
#     `Contents/Resources/lib` itself or wrote its own `Contents/lib`; there is no
#     such step today, and a future one must run before this line.
#     Both sweeps that enumerate files by magic bytes — the Mach-O-as-.py sanity
#     check and the signing loop — use `find <dir> -type f`, and `find` does not
#     follow symlinks without `-L`, so the link is neither signed as nested code
#     nor traversed a second time. Verified against a copy of an installed
#     bundle: adding the link and re-running `codesign --force --deep -s -` +
#     `codesign --verify --strict` gives "valid on disk / satisfies its
#     Designated Requirement", i.e. the bundle seal is happy with it (Apple's
#     bundle format seals symlinks through the resource rules).
#
#     RELATIVE (`Resources/lib`, not an absolute path): the .app is copied to
#     /Applications, mounted from a DMG, and run from a temp dir by CI. An
#     absolute link would point at whatever machine built it.
# ---------------------------------------------------------------------------

echo "==> making the bundled interpreter self-locating (Contents/lib -> Resources/lib)"
if [[ ! -d "$APP_DIR/Contents/Resources/lib" ]]; then
  echo "FATAL: $APP_DIR/Contents/Resources/lib does not exist — py2app's layout" >&2
  echo "       changed, so the landmark this symlink creates would point at" >&2
  echo "       nothing and the bundled interpreter could not self-locate." >&2
  exit 1
fi
# -f: py2app rebuilds into a fresh dist dir, but a re-run over an existing tree
# must not fail on (or nest inside) a link that is already there.
ln -sfn "Resources/lib" "$APP_DIR/Contents/lib"

# ---------------------------------------------------------------------------
# 4b. Bundle sanity checks.
#     a) No Mach-O binary masquerading as a .py: py2app's `packages` option
#        mis-copies a bare C-extension module (e.g. _duckdb) to
#        lib/python3.12/<name>.py, which shadows the real lib-dynload .so
#        and breaks the import with a null-byte SyntaxError.
#     b) `import duckdb` actually works through the app's own worker
#        (_child.py) — the exact path user UDFs take at runtime.
# ---------------------------------------------------------------------------

echo "==> bundle sanity: Mach-O-as-.py check"
APP_PYLIB="$APP_DIR/Contents/Resources/lib/python3.12"

# ---------------------------------------------------------------------------
# 4a-bis. Stage the packages py2app cannot carry (setup_py2app.STAGED_PACKAGES).
#     Today that is `google` (google-auth): a PEP 420 namespace package, which
#     py2app's package bootstrap cannot resolve, and naming its subpackages
#     dotted instead FAILS THE BUILD - collect_packagedirs() (build_app.py:1210)
#     maps get_bootstrap() over every `packages` entry and
#     modulegraph.util.imp_find_module then calls imp.find_module("google"),
#     which raises. So it is copied straight in, the same explicit staging that
#     rclone and uv get below. The list is read from setup_py2app.py so there is
#     exactly one declaration of it.
# ---------------------------------------------------------------------------

STAGED_PACKAGES="$("$BUILD_VENV/bin/python" "$REPO_ROOT/scripts/_staged_packages.py")"
if [[ -n "$STAGED_PACKAGES" ]]; then
  BUILD_SITE="$("$BUILD_VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  for pkg in $STAGED_PACKAGES; do
    SRC="$BUILD_SITE/$pkg"
    if [[ ! -d "$SRC" ]]; then
      echo "FATAL: staged package '$pkg' not found at $SRC" >&2
      exit 1
    fi
    echo "==> staging package $pkg into the bundle"
    rm -rf "$APP_PYLIB/$pkg"
    cp -R "$SRC" "$APP_PYLIB/$pkg"
    find "$APP_PYLIB/$pkg" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  done
  # Prove it IMPORTS through the bundled interpreter, the way a run would. A
  # copied directory python cannot import is exactly the failure this step
  # exists to prevent, and it is invisible without an actual import.
  # `|| true` INSIDE the substitution (the idiom `UV_SRC` uses below): without it
  # `set -euo pipefail` aborts on the ASSIGNMENT when the import fails — which is
  # the case this step exists for — so the ERR trap fires and the check plus the
  # `echo` below never run. The traceback `2>&1` just captured would be thrown
  # away and the operator told only "failed at line N". The grep IS the check.
  GOOGLE_SMOKE="$(env PYTHONHOME="$APP_DIR/Contents/Resources" \
    "$APP_DIR/Contents/MacOS/python" -c \
    'import google.auth, google.oauth2; print("google-auth OK", google.auth.__version__)' 2>&1 || true)"
  if ! echo "$GOOGLE_SMOKE" | grep -q "google-auth OK"; then
    echo "FATAL: staged google-auth does not import in the bundle:" >&2
    echo "$GOOGLE_SMOKE" >&2
    exit 1
  fi
  echo "    $GOOGLE_SMOKE"
fi

# find -exec ... {} + (not `xargs -I{}`, which aborts with "command line
# cannot be assembled, too long" over a large file set - see the signing loop
# below) enumerates >1M .py files whose first 4 bytes are a Mach-O magic.
BAD_PY=""
while IFS= read -r -d '' f; do
  BAD_PY+="$f"$'\n'
done < <(
  find "$APP_PYLIB" -name '*.py' -size +1M -exec sh -c '
    for f do
      case "$(head -c4 "$f" | xxd -p)" in
        cffaedfe|cafebabe|feedfacf) printf "%s\0" "$f" ;;
      esac
    done
  ' _ {} +
)
if [[ -n "$BAD_PY" ]]; then
  echo "FATAL: Mach-O binary shipped as .py (would shadow the real extension):" >&2
  echo "$BAD_PY" >&2
  exit 1
fi

echo "==> bundle sanity: duckdb import smoke test via _child.py"
SMOKE_DIR="$BUILD_DIR/smoke"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"
cat > "$SMOKE_DIR/duckdb_smoke.py" <<'PYEOF'
def main() -> dict:
    import duckdb
    con = duckdb.connect()
    return {
        "duckdb_version": duckdb.__version__,
        "answer": con.execute("SELECT 42").fetchone()[0],
    }
PYEOF
# `|| true` for the same reason as the google-auth smoke above: `set -e` would
# abort on the assignment and take the diagnostic with it.
# `pipefail` makes it doubly necessary here: the upstream `echo` counts too.
SMOKE_OUT="$(echo "{\"path\":\"$SMOKE_DIR/duckdb_smoke.py\",\"params\":{}}" | \
  env PYTHONHOME="$APP_DIR/Contents/Resources" \
  "$APP_DIR/Contents/MacOS/python" \
  "$APP_PYLIB/fused_render/_child.py" 2>&1 || true)"
if ! echo "$SMOKE_OUT" | grep -q '"ok": true'; then
  echo "FATAL: duckdb smoke test failed through _child.py:" >&2
  echo "$SMOKE_OUT" >&2
  exit 1
fi
echo "    $SMOKE_OUT"
rm -rf "$SMOKE_DIR"

# ---------------------------------------------------------------------------
# 4b-bis. The regression guard for 4a-ter: the bundled interpreter must locate
#     its own runtime with NO environment help.
#
#     Every other smoke above runs the bundled python WITH
#     `PYTHONHOME=Contents/Resources` — which is exactly why this bug shipped
#     invisibly. With PYTHONHOME set, a bundle whose landmark search resolves to
#     the build machine's Homebrew prefix passes every one of them. So this check
#     strips the four variables `python_compute` strips from its children
#     (PYTHONHOME/PYTHONPATH/VIRTUAL_ENV — PYTHONSTARTUP only affects interactive
#     sessions, so `-c` cannot see it) and asserts the two things that actually
#     matter:
#       a) `sys.prefix` is INSIDE this .app, not on some absolute path belonging
#          to the machine that built it. Compared against $APP_DIR by prefix,
#          because the build tree's path is not the install path;
#       b) the app's own package plus one heavy native package (duckdb, the same
#          one the _child.py smoke above uses) actually import — a prefix that
#          merely looks right is not proof that the runtime under it is complete.
#
#     A failure here is FATAL, not a warning: without this property, PEP 723
#     script venvs built from `Contents/MacOS/python` are unusable on every
#     user's machine (see 4a-ter), and the symptom — "python execution failed"
#     only for pages whose .py has a header — is nowhere near the cause.
# ---------------------------------------------------------------------------

echo "==> bundle sanity: interpreter self-locates with PYTHONHOME stripped"
# `|| true` inside the substitution for the same reason as the smokes above:
# `set -euo pipefail` would abort on the ASSIGNMENT — the very case this exists
# for — and take the diagnostic with it. The grep IS the check.
SELFLOC_OUT="$(env -u PYTHONHOME -u PYTHONPATH -u VIRTUAL_ENV \
  "$APP_DIR/Contents/MacOS/python" -c '
import sys
import fused_render, duckdb
print("prefix", sys.prefix)
print("selflocating OK", fused_render.__version__, duckdb.__version__)
' 2>&1 || true)"
if ! echo "$SELFLOC_OUT" | grep -q "^selflocating OK"; then
  echo "FATAL: the bundled interpreter cannot run without PYTHONHOME:" >&2
  echo "$SELFLOC_OUT" >&2
  echo "       (is Contents/lib -> Resources/lib missing? see step 4a-ter)" >&2
  exit 1
fi
SELFLOC_PREFIX="$(echo "$SELFLOC_OUT" | sed -n 's/^prefix //p')"
if [[ "$SELFLOC_PREFIX" != "$APP_DIR"* ]]; then
  echo "FATAL: the bundled interpreter's sys.prefix is OUTSIDE the app:" >&2
  echo "       $SELFLOC_PREFIX" >&2
  echo "       (that is the BUILD MACHINE's python — it does not exist on a" >&2
  echo "        user's disk, so every PEP 723 script venv built from this" >&2
  echo "        interpreter would be dead on arrival. See step 4a-ter.)" >&2
  exit 1
fi
echo "    $(echo "$SELFLOC_OUT" | tail -1) (prefix $SELFLOC_PREFIX)"

# ---------------------------------------------------------------------------
# 4c. Bundled fused CLI (SPEC §19 DP-3): the `fused` package installed above
#     (a [bundled] requirement, so setup_py2app.py's derivation forces it)
#     ships in the bundle so Deploy works with zero setup. Two artifacts:
#     - Contents/Resources/bin/fused: a terminal wrapper over the SAME baked-in CLI
#       (bundled python + fused_render/_fused_cli.py shim), for the one-time
#       setup steps a modal can't do (`fused cloud setup`, `fused cloud
#       login`, `fused env create`). The Deploy modal's guidance names this
#       path when running packaged (deploy._setup_cli_hint).
#     - a smoke test invoking real CLI verbs through the shim, so a py2app
#       packaging gap (an untraced dynamic import, a dropped data dir - see
#       setup_py2app.py's fused-deps block) fails the BUILD, not the user's first
#       deploy. Runs before signing: the wrapper must exist before the seal.
# ---------------------------------------------------------------------------

echo "==> bundled fused CLI: terminal wrapper + smoke test"
# The wrapper lives under Contents/Resources/bin, NOT Contents/MacOS:
# everything in Contents/MacOS is nested CODE to codesign, and a shell
# script there cannot carry a normal code signature - the bundle seal then
# fails with "code object is not signed at all / In subcomponent: ...fused".
# A script under Resources is sealed by the resource rules instead, which is
# exactly what Apple's bundle format intends for helper scripts.
PYLIB_NAME="$(basename "$APP_PYLIB")"   # e.g. python3.12
WRAPPER_PATH="$APP_DIR/Contents/Resources/bin/fused"
mkdir -p "$(dirname "$WRAPPER_PATH")"
cat > "$WRAPPER_PATH" <<WRAPPER
#!/bin/sh
# fused CLI bundled with FusedRender.app - the same interpreter + fused
# package the app's Deploy button uses (fused_render/_fused_cli.py, SPEC §19).
# PYTHONHOME points the bundled python at its own runtime, exactly as the
# app's own smoke tests / py2app launcher do. PYTHONPATH is UNSET (env -u):
# this is meant to be run from a user's shell, and a developer's inherited
# PYTHONPATH would otherwise prepend onto the bundled interpreter's sys.path
# and shadow bundled packages (a different numpy/pydantic/fused) - the same
# hazard deploy.py scrubs when spawning an external interpreter.
HERE="\$(cd "\$(dirname "\$0")" && pwd)"        # .../Contents/Resources/bin
RES="\$(cd "\$HERE/.." && pwd)"                  # .../Contents/Resources
exec env -u PYTHONPATH PYTHONHOME="\$RES" "\$RES/../MacOS/python" "\$RES/lib/${PYLIB_NAME}/fused_render/_fused_cli.py" "\$@"
WRAPPER
chmod +x "$WRAPPER_PATH"

# --help imports the whole click command tree; `env list` (against an empty,
# isolated store) exercises the environments stack (pydantic models et al).
for probe in "--help" "share --help" "env list"; do
  if ! PROBE_OUT="$(env OPENFUSED_ENVS_FILE="$BUILD_DIR/smoke-envs.json" \
      "$WRAPPER_PATH" $probe 2>&1)"; then
    echo "FATAL: bundled fused CLI failed on: fused $probe" >&2
    echo "$PROBE_OUT" >&2
    echo "(a py2app packaging gap? see setup_py2app.py's fused packages block)" >&2
    exit 1
  fi
done
echo "    fused --help / share --help / env list OK"

# ---------------------------------------------------------------------------
# 4d. Bundle rclone (D103, staged in step 2b above) at the same
#     Contents/Resources/bin/ spot as the fused wrapper - a real Mach-O
#     binary, not a script, so it's picked up and signed like any other
#     nested binary by the signing sweep below (step 5), no extra rule
#     needed there. shell/mounts.py's rclone_bin() looks here first.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 4d-bis. Bundle uv (D176). NOT a convenience: the install loader (SPEC PY-18)
#     builds a per-script venv for the few templates whose deps are too heavy to
#     bundle, and on macOS it has no other way to do it. `fused`'s venv builder
#     prefers `uv venv` and falls back to `<python> -m venv` — and this bundle
#     ships NO `venv`, `ensurepip` or `pip` module at all (py2app copies only what
#     setup_py2app.py names). Measured on an installed DMG without uv on PATH:
#     `RuntimeError: Failed to create venv ...: No module named venv`. So without
#     this, opening a .pptx trades one dead end ("pip install python-pptx", which
#     a DMG user cannot do) for another.
#
#     uv is also the RIGHT mechanism rather than merely the available one:
#     upstream's venvs.py prefers it precisely because it needs no ensurepip
#     bootstrap, "which matters when `exe` is an embedded/packaged interpreter (a
#     py2app .app's python)". Verified against the mounted bundle: `uv venv
#     --python <Contents/MacOS/python>` succeeds and `uv pip install` into it
#     works, because uv constructs venvs itself instead of importing `venv`.
#
#     The Linux AppImage (build_linux_appimage.sh) and the Windows installer
#     (build_windows_installer.ps1) already ship uv next to their python; this
#     brings macOS in line. Copied from the build host's uv, like those two do.
# ---------------------------------------------------------------------------

echo "==> bundling uv"
UV_SRC="$(command -v uv || true)"
if [[ -z "$UV_SRC" ]]; then
  echo "FATAL: uv not found on PATH, but the bundle needs it: the install loader" >&2
  echo "       cannot build a script venv without it (no venv/ensurepip/pip in" >&2
  echo "       this bundle), so heavy-dependency templates would dead-end." >&2
  echo "       Install uv (https://docs.astral.sh/uv/) and re-run." >&2
  exit 1
fi
UV_DEST="$APP_DIR/Contents/Resources/bin/uv"
mkdir -p "$(dirname "$UV_DEST")"
cp "$UV_SRC" "$UV_DEST"
chmod +x "$UV_DEST"
# `|| true` for the same reason as the google-auth smoke above: `set -e` would
# abort on the assignment and take the diagnostic with it.
UV_SMOKE_OUT="$("$UV_DEST" --version || true)"
if ! echo "$UV_SMOKE_OUT" | grep -q "^uv "; then
  echo "FATAL: bundled uv failed to report its version:" >&2
  echo "$UV_SMOKE_OUT" >&2
  exit 1
fi
echo "    $UV_SMOKE_OUT"

echo "==> bundling rclone ${RCLONE_VERSION}"
RCLONE_DEST="$APP_DIR/Contents/Resources/bin/rclone"
mkdir -p "$(dirname "$RCLONE_DEST")"
cp "$RCLONE_STAGED_BIN" "$RCLONE_DEST"
chmod +x "$RCLONE_DEST"

# `|| true` for the same reason as the google-auth smoke above: `set -e` would
# abort on the assignment and take the diagnostic with it.
RCLONE_SMOKE_OUT="$("$RCLONE_DEST" version || true)"
if ! echo "$RCLONE_SMOKE_OUT" | head -1 | grep -q "rclone ${RCLONE_VERSION}"; then
  echo "FATAL: bundled rclone failed to report its version:" >&2
  echo "$RCLONE_SMOKE_OUT" >&2
  exit 1
fi
echo "    $(echo "$RCLONE_SMOKE_OUT" | head -1)"

# ---------------------------------------------------------------------------
# 4e. Bundle learn.zip (D123): the repo's core_apps/learn/ content ships as a single
#     zip at Contents/Resources/learn.zip, and shell/mounts.py's
#     ensure_learn_mount() mounts it read-only at startup via rclone's
#     archive backend (:archive:<path>, new in v1.74) — the bundled default
#     content is presented through the exact same mounts surface as remote
#     data. Built fresh every run (a stale zip from a previous build must
#     never ship). MUST run before signing (step 5): Resources content has
#     to exist before the bundle seal.
# ---------------------------------------------------------------------------

echo "==> bundling learn.zip"
LEARN_SRC="$REPO_ROOT/core_apps/learn"
if [[ ! -d "$LEARN_SRC" ]]; then
  echo "FATAL: $LEARN_SRC does not exist — the learn/ content is part of the app." >&2
  exit 1
fi
LEARN_DEST="$APP_DIR/Contents/Resources/learn.zip"
rm -f "$LEARN_DEST"
# -X drops resource-fork/extended-attr entries; .DS_Store excluded so a
# Finder-visited checkout builds the same zip as CI.
(cd "$LEARN_SRC" && zip -qr -X "$LEARN_DEST" . -x '.DS_Store' -x '*/.DS_Store')

# Smoke test with the just-bundled rclone: the exact binary the app ships
# must be able to list the exact zip the app ships — catches an rclone
# version bump that drops/renames the archive backend before it reaches a
# user's first launch.
if ! LEARN_SMOKE_OUT="$("$RCLONE_DEST" lsf ":archive:${LEARN_DEST}" 2>&1)"; then
  echo "FATAL: bundled rclone cannot read the bundled learn.zip via :archive: :" >&2
  echo "$LEARN_SMOKE_OUT" >&2
  exit 1
fi
echo "    learn.zip OK ($(echo "$LEARN_SMOKE_OUT" | wc -l | tr -d ' ') top-level entries)"

# Same treatment for the Sessions sub-app content (repo core_apps/sessions/ →
# Contents/Resources/sessions.zip, mounted by ensure_builtin_mounts).
echo "==> bundling sessions.zip"
SESSIONS_SRC="$REPO_ROOT/core_apps/sessions"
if [[ ! -d "$SESSIONS_SRC" ]]; then
  echo "FATAL: $SESSIONS_SRC does not exist — the sessions/ content is part of the app." >&2
  exit 1
fi
SESSIONS_DEST="$APP_DIR/Contents/Resources/sessions.zip"
rm -f "$SESSIONS_DEST"
(cd "$SESSIONS_SRC" && zip -qr -X "$SESSIONS_DEST" . -x '.DS_Store' -x '*/.DS_Store' -x '__pycache__/*' -x '*/__pycache__/*')

if ! SESSIONS_SMOKE_OUT="$("$RCLONE_DEST" lsf ":archive:${SESSIONS_DEST}" 2>&1)"; then
  echo "FATAL: bundled rclone cannot read the bundled sessions.zip via :archive: :" >&2
  echo "$SESSIONS_SMOKE_OUT" >&2
  exit 1
fi
echo "    sessions.zip OK ($(echo "$SESSIONS_SMOKE_OUT" | wc -l | tr -d ' ') top-level entries)"

# Same treatment for the Community marketplace content (repo core_apps/community/
# → Contents/Resources/community.zip, mounted by ensure_builtin_mounts).
echo "==> bundling community.zip"
COMMUNITY_SRC="$REPO_ROOT/core_apps/community"
if [[ ! -d "$COMMUNITY_SRC" ]]; then
  echo "FATAL: $COMMUNITY_SRC does not exist — the community/ content is part of the app." >&2
  exit 1
fi
COMMUNITY_DEST="$APP_DIR/Contents/Resources/community.zip"
rm -f "$COMMUNITY_DEST"
(cd "$COMMUNITY_SRC" && zip -qr -X "$COMMUNITY_DEST" . -x '.DS_Store' -x '*/.DS_Store' -x '__pycache__/*' -x '*/__pycache__/*')

if ! COMMUNITY_SMOKE_OUT="$("$RCLONE_DEST" lsf ":archive:${COMMUNITY_DEST}" 2>&1)"; then
  echo "FATAL: bundled rclone cannot read the bundled community.zip via :archive: :" >&2
  echo "$COMMUNITY_SMOKE_OUT" >&2
  exit 1
fi
echo "    community.zip OK ($(echo "$COMMUNITY_SMOKE_OUT" | wc -l | tr -d ' ') top-level entries)"

# ---------------------------------------------------------------------------
# 5. Code signing (D73, realizes the D35 hook). Two modes:
#
#    - Developer ID (recommended for distribution): signs the whole bundle
#      inside-out with the hardened runtime, a secure timestamp, and the
#      entitlements the bundled Python + native libs need. This is the
#      prerequisite for notarization (step 6b) AND the general form of the D72
#      Downloads-prompt fix: with one stable Team ID signing the app stub and
#      the nested `python` the executor spawns, macOS attributes that worker's
#      protected-folder access to the app, so the permission prompt appears
#      once for the app instead of once per subprocess (covers user code too,
#      not just the in-process built-in readers).
#    - Ad-hoc (default when no identity is configured): unchanged prior
#      behavior. py2app already ad-hoc signs on Apple Silicon (unsigned
#      binaries won't launch), but we re-sign deterministically over the whole
#      bundle. Launches locally (right-click -> Open once); not distributable
#      without Gatekeeper warnings.
#
#    Identity resolution is keychain-based (see docs/signing.md):
#      FUSED_RENDER_CODESIGN_IDENTITY  explicit identity (a "Developer ID
#                                      Application: NAME (TEAMID)" string or a
#                                      cert SHA-1). If unset, auto-detect a
#                                      single "Developer ID Application" cert
#                                      from the keychain; several -> stop and
#                                      ask; none -> ad-hoc fallback.
#      FUSED_RENDER_CODESIGN_KEYCHAIN  optional keychain to search / sign from
#                                      (a dedicated, unlocked keychain in CI).
#                                      Path must not contain spaces.
# ---------------------------------------------------------------------------

# Escape hatch for size-measurement / dev iteration builds: skip signing
# entirely (both Developer ID and ad-hoc). The resulting app may not LAUNCH
# on Apple Silicon (unsigned binaries are refused), but the DMG is byte-for-
# byte comparable for size work and the pre-sign smoke tests above still
# validate the bundle. Never use for a distributable build.
# Enabled only by the exact value "1" — a leftover "0"/"false" in the
# environment must not silently ship an unsigned build.
if [[ "${FUSED_RENDER_SKIP_CODESIGN:-}" == "1" ]]; then
  echo "==> FUSED_RENDER_SKIP_CODESIGN set -> skipping codesign (measurement build)"
  SIGN_IDENTITY=""
else

KC_PATH="${FUSED_RENDER_CODESIGN_KEYCHAIN:-}"
KC_OPT=""
[[ -n "$KC_PATH" ]] && KC_OPT="--keychain $KC_PATH"

SIGN_IDENTITY=""
if [[ -n "${FUSED_RENDER_CODESIGN_IDENTITY:-}" ]]; then
  SIGN_IDENTITY="$FUSED_RENDER_CODESIGN_IDENTITY"
else
  # `security find-identity` takes the keychain as a positional arg (empty
  # expands to nothing = search the default list). grep|sed pulls the cert
  # SHA-1 (unambiguous, unlike the display name) out of a line like:
  #   1) A1B2..F "Developer ID Application: Jane Dev (TEAMID)"
  IDENTITY_LINES="$(security find-identity -v -p codesigning $KC_PATH 2>/dev/null \
    | grep 'Developer ID Application' || true)"
  IDENTITY_COUNT="$(printf '%s' "$IDENTITY_LINES" | grep -c 'Developer ID Application' || true)"
  if [[ "$IDENTITY_COUNT" -eq 1 ]]; then
    SIGN_IDENTITY="$(printf '%s\n' "$IDENTITY_LINES" | sed -E 's/^ *[0-9]+\) +([0-9A-Fa-f]+) .*/\1/')"
  elif [[ "$IDENTITY_COUNT" -gt 1 ]]; then
    echo "FATAL: multiple 'Developer ID Application' identities in the keychain." >&2
    echo "       Set FUSED_RENDER_CODESIGN_IDENTITY to pick one of:" >&2
    printf '%s\n' "$IDENTITY_LINES" >&2
    exit 1
  fi
fi

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "==> Developer ID signing (hardened runtime): $SIGN_IDENTITY"

  # Entitlements the bundled interpreter + native stack need under the
  # hardened runtime. Build artifact, not committed.
  ENTITLEMENTS="$BUILD_DIR/entitlements.plist"
  cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <!-- The bundled CPython loads third-party native libs (numpy, pyarrow,
       duckdb, ...) not signed by our Team; without this, hardened-runtime
       library validation refuses to load them. -->
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <!-- Python and some numeric/JIT libs allocate & execute code at runtime. -->
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <!-- app.py points the bundled interpreter/worker at its own runtime via
       PYTHONHOME etc. -->
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict>
</plist>
PLIST

  # Inside-out: every nested Mach-O must carry a valid Developer ID signature +
  # hardened runtime + timestamp before the enclosing .app, or notarization
  # rejects the bundle. --deep is deliberately NOT used (Apple advises against
  # it for distribution and it skips nested executables like the bundled
  # `python`); we enumerate every Mach-O by magic bytes (same detection as the
  # sanity check above) and sign each, then seal the .app last.
  echo "==> signing nested Mach-O binaries"
  # find -exec ... {} + emits NUL-delimited Mach-O paths, read back with
  # `read -d ''`. NOT `find -print0 | xargs -0 -I{} sh -c ...`: BSD xargs -I{}
  # aborts with "command line cannot be assembled, too long" over the ~375
  # Mach-O files in this bundle and, because the old `|| true` swallowed the
  # failure, the loop ran on an EMPTY list - shipping ad-hoc-signed nested
  # dylibs that notarization then rejected ("not signed with a valid Developer
  # ID certificate" / "does not include a secure timestamp").
  signed_count=0
  while IFS= read -r -d '' macho; do
    codesign --force --options runtime --timestamp \
      --entitlements "$ENTITLEMENTS" $KC_OPT -s "$SIGN_IDENTITY" "$macho"
    signed_count=$((signed_count + 1))
  done < <(
    find "$APP_DIR" -type f -exec sh -c '
      for f do
        case "$(head -c4 "$f" | xxd -p)" in
          cffaedfe|cafebabe|feedfacf|feedface|cefaedfe|bebafeca) printf "%s\0" "$f" ;;
        esac
      done
    ' _ {} +
  )
  echo "    signed $signed_count nested binaries"
  if [[ "$signed_count" -eq 0 ]]; then
    echo "FATAL: found no nested Mach-O binaries to sign — detection is broken;" >&2
    echo "       refusing to seal a bundle whose nested code is unsigned." >&2
    exit 1
  fi
  echo "==> sealing the app bundle"
  codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" $KC_OPT -s "$SIGN_IDENTITY" "$APP_DIR"
  codesign --verify --strict --verbose=2 "$APP_DIR"
else
  echo "==> no Developer ID identity configured -> ad-hoc codesigning (local use only)"
  echo "    Set FUSED_RENDER_CODESIGN_IDENTITY, or add a Developer ID Application"
  echo "    cert to your keychain, to produce a distributable build. See docs/signing.md."
  codesign --force --deep -s - "$APP_DIR"
fi

fi  # FUSED_RENDER_SKIP_CODESIGN

# ---------------------------------------------------------------------------
# 6. DMG via dmgbuild: app + Applications symlink, compressed ULFO
# ---------------------------------------------------------------------------

echo "==> building dmg"
DMGBUILD_SETTINGS="$BUILD_DIR/dmgbuild_settings.py"
cat > "$DMGBUILD_SETTINGS" <<'PYEOF'
# Generated by build_dmg.sh - not committed.
import os

application = defines.get("app")  # noqa: F821 (dmgbuild injects `defines`)
appname = os.path.basename(application)

files = [application]
symlinks = {"Applications": "/Applications"}
# ULFO (LZFSE) compresses this bundle notably tighter than the classic UDZO
# (zlib) and mounts faster; needs macOS 10.11+ to open, well under the app's
# own LSMinimumSystemVersion of 11.0 (setup_py2app.py).
format = "ULFO"
PYEOF

rm -f "$DMG_PATH"
"$BUILD_VENV/bin/dmgbuild" -s "$DMGBUILD_SETTINGS" -D app="$APP_DIR" "$APP_NAME" "$DMG_PATH"

# ---------------------------------------------------------------------------
# 6b. Notarize + staple (optional, D73). Runs only when a notarytool keychain
#     profile is configured; requires a Developer ID signature (ad-hoc can't be
#     notarized). Submits the finished DMG, waits for Apple's verdict, and
#     staples the ticket so the DMG passes Gatekeeper on download with no
#     network round-trip; the app it carries is notarized and launches without
#     the right-click -> Open dance.
#
#     Store the profile once, then it lives in the keychain:
#       xcrun notarytool store-credentials FUSED_RENDER_NOTARY \
#         --apple-id you@example.com --team-id TEAMID --password <app-specific-pw>
#     (or --key/--key-id/--issuer for an App Store Connect API key). Then:
#       FUSED_RENDER_NOTARY_PROFILE=FUSED_RENDER_NOTARY bash scripts/build_dmg.sh
#     See docs/signing.md.
# ---------------------------------------------------------------------------

if [[ -n "${FUSED_RENDER_NOTARY_PROFILE:-}" ]]; then
  if [[ "${FUSED_RENDER_SKIP_CODESIGN:-}" == "1" ]]; then
    echo "FATAL: FUSED_RENDER_NOTARY_PROFILE and FUSED_RENDER_SKIP_CODESIGN are both set —" >&2
    echo "       the app is completely unsigned; there is nothing to notarize." >&2
    exit 1
  fi
  if [[ -z "$SIGN_IDENTITY" ]]; then
    echo "FATAL: FUSED_RENDER_NOTARY_PROFILE is set but the app was signed ad-hoc." >&2
    echo "       Notarization requires a Developer ID signature — configure" >&2
    echo "       FUSED_RENDER_CODESIGN_IDENTITY (see docs/signing.md)." >&2
    exit 1
  fi
  echo "==> notarizing $DMG_PATH (profile: $FUSED_RENDER_NOTARY_PROFILE)"
  xcrun notarytool submit "$DMG_PATH" \
    --keychain-profile "$FUSED_RENDER_NOTARY_PROFILE" --wait
  echo "==> stapling notarization ticket"
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
else
  echo "==> skipping notarization (FUSED_RENDER_NOTARY_PROFILE unset)"
fi

# ---------------------------------------------------------------------------
# 7. Hygiene: drop the built .app copies once they're sealed in the DMG.
#    Leaving live .app bundles sitting in a gitignored build/ dir is a
#    Spotlight/Time Machine indexing trap and an easy source of "which copy
#    did I actually test" confusion; the DMG in dist/ is the deliverable.
#    (The build venv is kept - it's a build tool, not a build artifact, and
#    keeping it makes repeat builds much faster.)
# ---------------------------------------------------------------------------

rm -rf "$APP_DIR" "$ICONSET_DIR"

echo "==> done: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
