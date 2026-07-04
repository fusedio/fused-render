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
# real, re-invokable interpreter, so the subprocess executor
# (executor.py/_child.py) keeps working completely unchanged (D33).
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
# 2. Build venv: fused-render[bundled,app] + py2app + dmgbuild
# ---------------------------------------------------------------------------

if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
  echo "==> creating build venv"
  "$FRAMEWORK_PYTHON" -m venv "$BUILD_VENV"
fi

echo "==> installing fused-render[bundled,app] + py2app + dmgbuild into the build venv"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
"$BUILD_VENV/bin/pip" install --quiet "${REPO_ROOT}[bundled,app]" py2app dmgbuild

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
  FUSED_RENDER_ICNS="$ICNS_PATH" "$BUILD_VENV/bin/python" "$REPO_ROOT/scripts/setup_py2app.py" py2app \
    --dist-dir "$PY2APP_DIST" \
    --bdist-base "$BUILD_DIR/py2app-build"
)

test -d "$APP_DIR"

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
