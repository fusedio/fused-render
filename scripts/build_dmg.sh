#!/usr/bin/env bash
# Build FusedRender.app + a distributable DMG via py2app (SPEC §12, D33-D35).
#
# Pipeline: pick/bootstrap a FRAMEWORK-build python (py2app needs one to
# produce a real standalone bundle, see the note below) -> build a venv on it
# -> pip install fused-render[bundled,app] + py2app + dmgbuild into that venv
# -> generate the app icon -> run py2app -> codesign -> dmgbuild -> notarize.
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$(PYTHONPATH="$REPO_ROOT" python3 -m fused_render._branch ref)"
SUFFIX="$(PYTHONPATH="$REPO_ROOT" python3 -m fused_render._branch suffix)"
APP_NAME="FusedRender${SUFFIX}"

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
# 2. Build venv: fused-render[bundled,app,fused] + py2app + dmgbuild
# ---------------------------------------------------------------------------

if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
  echo "==> creating build venv"
  "$FRAMEWORK_PYTHON" -m venv "$BUILD_VENV"
fi

echo "==> installing fused-render[bundled,app,fused] + py2app + dmgbuild into the build venv"
export FUSED_RENDER_BRANCH="$REF"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
# [fused] bakes the deploy CLI into the bundle (SPEC §19 DP-3): the .app has
# no pip and no console scripts, so the Deploy surface runs the package
# in-interpreter via fused_render/_fused_cli.py under the bundled python.
"$BUILD_VENV/bin/pip" install --quiet "${REPO_ROOT}[bundled,app,fused]" py2app dmgbuild
# Force a fresh rebuild+reinstall of fused-render itself every run so the branch
# ref is re-baked to match $REF. The build venv is reused across builds, so pip
# would otherwise treat an unchanged version as already-satisfied (or reuse a
# cached wheel) and ship a stale _baked_branch.py from a previous ref.
"$BUILD_VENV/bin/pip" install --quiet --force-reinstall --no-deps --no-cache-dir "${REPO_ROOT}"

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
# 4c. Bundled fused CLI (SPEC §19 DP-3): the [fused] extra installed above
#     ships in the bundle so Deploy works with zero setup. Two artifacts:
#     - Contents/Resources/bin/fused: a terminal wrapper over the SAME baked-in CLI
#       (bundled python + fused_render/_fused_cli.py shim), for the one-time
#       setup steps a modal can't do (`fused cloud setup`, `fused cloud
#       login`, `fused env create`). The Deploy modal's guidance names this
#       path when running packaged (deploy._setup_cli_hint).
#     - a smoke test invoking real CLI verbs through the shim, so a py2app
#       packaging gap (an untraced dynamic import, a dropped data dir - see
#       setup_py2app.py's fused block) fails the BUILD, not the user's first
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
