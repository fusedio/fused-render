#!/usr/bin/env bash
# Build FusedRender.app + a distributable DMG via py2app (SPEC §12, D33-D35).
#
# Pipeline: pick/bootstrap a FRAMEWORK-build python (py2app needs one to
# produce a real standalone bundle, see the note below) -> build a venv on it
# -> pip install fused-render[bundled,app] + py2app + dmgbuild into that venv
# -> generate the app icon -> run py2app -> codesign -> dmgbuild -> notarize.
#
# Signing (step 5, D69) is credential-driven: with a Developer ID identity in
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
BAD_PY="$(find "$APP_PYLIB" -name '*.py' -size +1M -print0 | \
  xargs -0 -I{} sh -c 'head -c4 "{}" | xxd -p | grep -qE "^(cffaedfe|cafebabe|feedfacf)$" && echo "{}"' || true)"
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
SMOKE_OUT="$(echo "{\"path\":\"$SMOKE_DIR/duckdb_smoke.py\",\"params\":{}}" | \
  env PYTHONHOME="$APP_DIR/Contents/Resources" \
  "$APP_DIR/Contents/MacOS/python" \
  "$APP_PYLIB/fused_render/_child.py")"
if ! echo "$SMOKE_OUT" | grep -q '"ok": true'; then
  echo "FATAL: duckdb smoke test failed through _child.py:" >&2
  echo "$SMOKE_OUT" >&2
  exit 1
fi
echo "    $SMOKE_OUT"
rm -rf "$SMOKE_DIR"

# ---------------------------------------------------------------------------
# 5. Code signing (D69, realizes the D35 hook). Two modes:
#
#    - Developer ID (recommended for distribution): signs the whole bundle
#      inside-out with the hardened runtime, a secure timestamp, and the
#      entitlements the bundled Python + native libs need. This is the
#      prerequisite for notarization (step 6b) AND the general form of the D68
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
  while IFS= read -r macho; do
    [[ -z "$macho" ]] && continue
    codesign --force --options runtime --timestamp \
      --entitlements "$ENTITLEMENTS" $KC_OPT -s "$SIGN_IDENTITY" "$macho"
  done < <(
    find "$APP_DIR" -type f -print0 \
      | xargs -0 -I{} sh -c \
        'head -c4 "{}" | xxd -p | grep -qE "^(cffaedfe|cafebabe|feedfacf|feedface|cefaedfe|bebafeca)$" && echo "{}"' \
      || true
  )
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
# 6b. Notarize + staple (optional, D69). Runs only when a notarytool keychain
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
