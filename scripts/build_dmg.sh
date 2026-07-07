#!/usr/bin/env bash
# Build FusedRender.app + a distributable DMG via py2app (SPEC §12, D33-D35).
#
# Pipeline: pick/bootstrap a FRAMEWORK-build python (py2app needs one to
# produce a real standalone bundle, see the note below) -> build a venv on it
# -> pip install fused-render[bundled,app] + py2app + dmgbuild into that venv
# -> generate the app icon -> run py2app -> ad-hoc codesign -> dmgbuild.
#
# This replaces the earlier hand-rolled tarball-shim assembly (D29-D32): that
# approach's bare bash-shim launch was the likely cause of flaky
# NSStatusItem/AppKit behavior under Finder launches. py2app's compiled stub
# executable gives the process proper LaunchServices/AppKit identity, and
# modern py2app still ships a REAL python interpreter in the bundle (not a
# PyInstaller-style freeze) — `sys.executable` inside the running app is a
# real, re-invokable interpreter. openfused's local backend (which now runs
# all user scripts, see fused_render/engine.py) depends on exactly that: it
# creates per-script venvs via `sys.executable -m venv` and re-invokes the
# interpreter to run user code — impossible from a frozen single-binary (D33).
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="FusedRender"

VERSION="$(python3 -c "
import re
text = open('${REPO_ROOT}/pyproject.toml').read()
print(re.search(r'(?m)^version\s*=\s*\"([^\"]+)\"', text).group(1))
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
# 2. Build venv: fused-render[app] + build tools.
#
#    The [bundled] extra is deliberately NOT installed: user scripts run in
#    openfused-managed venvs (D56) and cannot import from the bundled
#    interpreter's site-packages, so baking the full data stack in only
#    bloated the .app. Scripts get those packages from the in-bundle
#    wheelhouse instead (§2b, D58) — the [bundled] list in pyproject.toml
#    lives on as that wheelhouse's manifest. Note the ceiling: the fused
#    dist itself requires numpy/pandas/pyarrow/duckdb/requests, so those
#    still ship as engine dependencies; what this saves is the rest
#    (matplotlib, scipy, polars, shapely/geopandas, openpyxl, pillow).
#    pillow is installed as a build TOOL (icon generation below); nothing
#    in the app imports it, so py2app doesn't ship it.
#
#    The venv is recreated per build: an incremental venv reused across a
#    dependency-set change would let py2app sweep stale packages into the
#    bundle. pip's wheel cache keeps the rebuild fast.
# ---------------------------------------------------------------------------

echo "==> creating build venv (fresh)"
rm -rf "$BUILD_VENV"
"$FRAMEWORK_PYTHON" -m venv "$BUILD_VENV"

echo "==> installing fused-render[app] + build tools into the build venv"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
"$BUILD_VENV/bin/pip" install --quiet "${REPO_ROOT}[app]" py2app dmgbuild pillow

# ---------------------------------------------------------------------------
# 2b. Wheelhouse: wheels for the blessed data stack, shipped inside the .app
#     at Contents/Resources/wheels (via setup_py2app.py's `resources`).
#
#     The openfused engine installs each user script's PEP 723 dependencies
#     into a cached venv at first use, and those install subprocesses inherit
#     the app process's environment — fused_render/app.py points
#     PIP_FIND_LINKS/UV_FIND_LINKS at this directory, so first use of the
#     popular stack resolves locally and works offline, with PyPI as fallback
#     when online (find-links, deliberately NOT --no-index hard-offline mode).
#
#     The set = the [bundled] extra's packages (read live from pyproject.toml
#     so the two can't drift) + pyarrow (the parquet readers need it and it is
#     no longer a core dep). Downloading with the build venv's pip guarantees
#     cp312/arm64-compatible wheels (that venv IS the bundled interpreter);
#     transitive deps are included by pip download's default resolution.
#     --only-binary keeps sdists out: an offline bare venv could not build one.
# ---------------------------------------------------------------------------

echo "==> building wheelhouse"
WHEELS_DIR="$BUILD_DIR/wheels"
rm -rf "$WHEELS_DIR"
mkdir -p "$WHEELS_DIR"
WHEEL_PKGS="$("$BUILD_VENV/bin/python" -c "
import tomllib
with open('${REPO_ROOT}/pyproject.toml', 'rb') as f:
    deps = tomllib.load(f)['project']['optional-dependencies']['bundled']
print(' '.join(deps))
")"
# shellcheck disable=SC2086  # word-splitting WHEEL_PKGS is the point
"$BUILD_VENV/bin/pip" download --quiet --only-binary=:all: \
  --dest "$WHEELS_DIR" $WHEEL_PKGS pyarrow
echo "    $(find "$WHEELS_DIR" -name '*.whl' | wc -l | tr -d ' ') wheels in $WHEELS_DIR"

# ---------------------------------------------------------------------------
# 2c. Bundled uv: the installer the app's venv builds MUST use.
#
#     The py2app stub launches everything with PYTHONHOME=Contents/Resources,
#     and openfused's install subprocesses inherit that environment. Under an
#     inherited PYTHONHOME, `<venv>/bin/python -m pip install` resolves its
#     prefix from PYTHONHOME instead of the venv: pip "installs" into the app
#     bundle, exits 0, and openfused then marks the EMPTY venv ready — every
#     later run of that script fails with ModuleNotFoundError from a poisoned
#     cache entry. uv is immune (it resolves the --python target venv itself,
#     without running Python's prefix detection), but Finder-launched apps get
#     a minimal PATH with no uv on it — so we ship uv in the bundle and
#     fused_render/app.py prepends Contents/Resources/appbin to PATH. The uv
#     binary comes from the uv wheel installed into the build venv (arm64,
#     matches the bundle).
# ---------------------------------------------------------------------------

echo "==> bundling uv"
BIN_DIR="$BUILD_DIR/appbin"
rm -rf "$BIN_DIR"
mkdir -p "$BIN_DIR"
"$BUILD_VENV/bin/pip" install --quiet uv
cp "$BUILD_VENV/bin/uv" "$BIN_DIR/uv"
"$BIN_DIR/uv" --version >/dev/null  # runnable, right arch
echo "    $("$BIN_DIR/uv" --version)"

# ---------------------------------------------------------------------------
# 2d. Bundled standalone CPython: the base interpreter for user-script venvs
#     (D67), shipped to Contents/Resources/python-standalone.
#
#     openfused bases its cached venvs on a python executable. The frozen
#     py2app interpreter cannot play that role reliably: stdlib `venv` strips
#     PYTHONHOME from the ensurepip child (and the runner strips it from
#     user-code children), leaving the frozen binary to self-locate its
#     stdlib — machine-dependent, and on machines where it fails every venv
#     creation exits 1 instantly (real DMG user report). python-build-
#     standalone is CPython built to be relocatable: standard bin/../lib
#     layout, self-locates with no PYTHONHOME from any path (verified even
#     under `env -i`). Version MUST stay cp312 to match the wheelhouse (§2b).
#     fused_render/engine.py auto-detects this directory and passes it to the
#     backend as python_executable.
#
#     The tarball is pinned (URL + sha256) and cached under build/; the
#     `install_only_stripped` variant is the full interpreter minus debug
#     symbols (~24 MB compressed).
# ---------------------------------------------------------------------------

echo "==> bundling standalone CPython (venv base, D67)"
PBS_TAG="20260623"
PBS_VER="3.12.13"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PBS_VER}%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_SHA256="41df7d3ae4757e84b97874f76d634268456aaa271740d33f968d826374998fb7"
PBS_TARBALL="$BUILD_DIR/cpython-${PBS_VER}+${PBS_TAG}-aarch64-install_only_stripped.tar.gz"
PBS_DIR="$BUILD_DIR/python-standalone"

if [[ ! -f "$PBS_TARBALL" ]] || ! echo "$PBS_SHA256  $PBS_TARBALL" | shasum -a 256 -c - >/dev/null 2>&1; then
  curl -fsSL -o "$PBS_TARBALL" "$PBS_URL"
fi
echo "$PBS_SHA256  $PBS_TARBALL" | shasum -a 256 -c - >/dev/null
rm -rf "$PBS_DIR"
mkdir -p "$PBS_DIR"
# Tarball's top-level dir is python/ — extract its contents directly into
# python-standalone/ so the bundle path is Resources/python-standalone/bin/...
tar xzf "$PBS_TARBALL" -C "$PBS_DIR" --strip-components 1
"$PBS_DIR/bin/python3" -c "import sys; assert sys.version_info[:2] == (3, 12)"
echo "    $("$PBS_DIR/bin/python3" --version) at $PBS_DIR"

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

draw.polygon(poly, fill=(91, 157, 255, 255))  # --accent

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
(
  cd "$BUILD_DIR"
  FUSED_RENDER_ICNS="$ICNS_PATH" FUSED_RENDER_WHEELS="$WHEELS_DIR" FUSED_RENDER_BIN="$BIN_DIR" \
    FUSED_RENDER_PYSTANDALONE="$PBS_DIR" \
    "$BUILD_VENV/bin/python" "$REPO_ROOT/scripts/setup_py2app.py" py2app \
    --dist-dir "$PY2APP_DIST" \
    --bdist-base "$BUILD_DIR/py2app-build"
)

test -d "$APP_DIR"

# ---------------------------------------------------------------------------
# 4b. Bundle sanity checks.
#     a) No Mach-O binary masquerading as a .py: py2app's `packages` option
#        mis-copies a bare C-extension module (e.g. _duckdb) to
#        lib/python3.12/<name>.py, which shadows the real lib-dynload .so
#        and breaks the import with a null-byte SyntaxError.
#     b) The bundled interpreter runs a script end-to-end through the
#        openfused engine (fused_render.engine.run_python): the fused package
#        imports from inside the bundle, and — the load-bearing part —
#        openfused can create its bare venv by re-invoking
#        `sys.executable -m venv` from the bundle (the D29-class risk).
#        The script is stdlib-only result-style, so no install/network runs.
#     c) The wheelhouse shipped: Contents/Resources/wheels holds >0 wheels,
#        including pyarrow.
# ---------------------------------------------------------------------------

echo "==> bundle sanity: Mach-O-as-.py check"
APP_PYLIB="$APP_DIR/Contents/Resources/lib/python3.12"
BAD_PY="$(find "$APP_PYLIB" -name '*.py' -size +1M -print0 | \
  xargs -0 -I{} sh -c 'head -c4 "{}" | xxd -p | grep -qE "^(cffaedfe|cafebabe|feedfacf)$" && echo "{}"' || true)"
if [[ -n "$BAD_PY" ]]; then
  echo "FATAL: Mach-O binary shipped as .py (would shadow the real extension):" >&2
  echo "$BAD_PY" >&2
  exit 1
fi

echo "==> bundle sanity: engine smoke test via the bundled interpreter"
SMOKE_DIR="$BUILD_DIR/smoke"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR/home"
cat > "$SMOKE_DIR/engine_smoke.py" <<'PYEOF'
import platform

result = {"smoke": "ok", "python": platform.python_version()}
PYEOF
# HOME is redirected into the smoke dir: openfused caches its venvs under
# ~/.openfused/venvs keyed by interpreter path, and this build .app's path is
# deleted in step 7 — don't leave a dead cached venv in the real HOME.
SMOKE_OUT="$(env PYTHONHOME="$APP_DIR/Contents/Resources" HOME="$SMOKE_DIR/home" \
  "$APP_DIR/Contents/MacOS/python" -c "
import asyncio, json, sys
from fused_render import engine
out = asyncio.run(engine.run_python('$SMOKE_DIR/engine_smoke.py', {}))
if out['error'] is not None:
    sys.exit('engine error: %s' % out['error'])
rv = out['return_value']
if isinstance(rv, str):
    rv = json.loads(rv)
if not rv or rv.get('smoke') != 'ok':
    sys.exit('unexpected return_value: %r' % (out['return_value'],))
print('SMOKE_OK', json.dumps(rv))
" 2>&1 || true)"
if ! echo "$SMOKE_OUT" | grep -q 'SMOKE_OK'; then
  echo "FATAL: engine smoke test failed through the bundled interpreter:" >&2
  echo "$SMOKE_OUT" >&2
  exit 1
fi
echo "    $(echo "$SMOKE_OUT" | grep 'SMOKE_OK')"

# d) PEP 723 install smoke under FINDER-LAUNCH conditions: PYTHONHOME set
#    (the stub sets it), a minimal GUI PATH with ONLY the bundled uv added
#    (as app.py's PATH prepend produces), find-links at the bundle wheels
#    (as app.py sets them). This is the regression test for the poisoned-venv
#    bug: with pip instead of uv, this install succeeds vacuously (pip
#    resolves its prefix from PYTHONHOME, "installs" into the bundle, exits
#    0) and the run then dies with ModuleNotFoundError from a cached empty
#    venv. openpyxl is used because it's small and already in the wheelhouse.
echo "==> bundle sanity: PEP 723 install smoke (GUI-launch env, bundled uv)"
cat > "$SMOKE_DIR/reqs_smoke.py" <<'PYEOF'
# /// script
# dependencies = ["openpyxl"]
# ///
import openpyxl

result = {"openpyxl": openpyxl.__version__}
PYEOF
REQS_OUT="$(env PYTHONHOME="$APP_DIR/Contents/Resources" HOME="$SMOKE_DIR/home" \
  PATH="$APP_DIR/Contents/Resources/appbin:/usr/bin:/bin" \
  PIP_FIND_LINKS="$APP_DIR/Contents/Resources/wheels" \
  UV_FIND_LINKS="$APP_DIR/Contents/Resources/wheels" \
  "$APP_DIR/Contents/MacOS/python" -c "
import asyncio, json, sys
from fused_render import engine
out = asyncio.run(engine.run_python('$SMOKE_DIR/reqs_smoke.py', {}))
if out['error'] is not None:
    sys.exit('engine error: %s' % out['error'])
rv = out['return_value']
if isinstance(rv, str):
    rv = json.loads(rv)
if not rv or not rv.get('openpyxl'):
    sys.exit('unexpected return_value: %r' % (out['return_value'],))
print('REQS_SMOKE_OK', json.dumps(rv))
" 2>&1 || true)"
if ! echo "$REQS_OUT" | grep -q 'REQS_SMOKE_OK'; then
  echo "FATAL: PEP 723 install smoke failed (poisoned-venv regression?):" >&2
  echo "$REQS_OUT" >&2
  exit 1
fi
echo "    $(echo "$REQS_OUT" | grep 'REQS_SMOKE_OK')"

# e) Standalone interpreter shipped intact and actually used (D67): py2app's
#    resource copy must preserve exec bits/symlinks well enough that the
#    bundled python runs, and the venvs the smokes above created must be
#    BASED on it (pyvenv.cfg home points into python-standalone) — not on the
#    frozen py2app interpreter. This is the regression gate for the
#    ensurepip-exit-1 class of DMG failures.
echo "==> bundle sanity: standalone venv-base python (D67)"
APP_PBS="$APP_DIR/Contents/Resources/python-standalone/bin/python3"
if ! "$APP_PBS" -c "import sys; assert sys.version_info[:2] == (3, 12)"; then
  echo "FATAL: bundled python-standalone does not run from inside the .app" >&2
  exit 1
fi
if ! grep -rq "python-standalone" "$SMOKE_DIR"/home/.openfused/venvs/*/pyvenv.cfg; then
  echo "FATAL: smoke venvs were not based on the bundled standalone python:" >&2
  cat "$SMOKE_DIR"/home/.openfused/venvs/*/pyvenv.cfg >&2
  exit 1
fi
echo "    venv base: $("$APP_PBS" --version) (python-standalone)"
rm -rf "$SMOKE_DIR"

echo "==> bundle sanity: wheelhouse shipped"
BUNDLE_WHEELS="$APP_DIR/Contents/Resources/wheels"
WHL_COUNT="$(find "$BUNDLE_WHEELS" -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$WHL_COUNT" -eq 0 ]]; then
  echo "FATAL: no wheels found in $BUNDLE_WHEELS" >&2
  exit 1
fi
if ! find "$BUNDLE_WHEELS" -name 'pyarrow-*.whl' | grep -q .; then
  echo "FATAL: pyarrow wheel missing from $BUNDLE_WHEELS" >&2
  exit 1
fi
echo "    $WHL_COUNT wheels (pyarrow present)"

# ---------------------------------------------------------------------------
# 5. Ad-hoc codesign (D32/DM-4 carried forward). py2app signs ad-hoc on its
#    own on Apple Silicon (unsigned binaries won't launch at all), but we
#    re-sign explicitly here so the signature is deterministic and covers the
#    whole bundle regardless of py2app version behavior. No Developer ID yet
#    - testers right-click -> Open once. Real signing + notarization has a
#    designated future home: Briefcase's "external app" packaging mode
#    (wraps an already-built .app for signing/notarizing/DMG without
#    reprocessing it through Briefcase's own app template, which breaks
#    sys.executable - see D35). Not built here.
# ---------------------------------------------------------------------------

echo "==> ad-hoc codesigning"
codesign --force --deep -s - "$APP_DIR"

# --- future hook: Developer ID signing + notarization ------------------------
# codesign --force --deep --options runtime \
#   -s "Developer ID Application: Your Name (TEAMID)" "$APP_DIR"
# xcrun notarytool submit "$DMG_PATH" --keychain-profile "fused-render" --wait
# xcrun stapler staple "$APP_DIR"
# briefcase package --target external-app  # future signing path, see D35
# ------------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. DMG via dmgbuild: app + Applications symlink, compressed UDZO
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
format = "UDZO"
PYEOF

rm -f "$DMG_PATH"
"$BUILD_VENV/bin/dmgbuild" -s "$DMGBUILD_SETTINGS" -D app="$APP_DIR" "$APP_NAME" "$DMG_PATH"

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
