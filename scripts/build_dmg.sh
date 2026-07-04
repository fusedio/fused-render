#!/usr/bin/env bash
# Build FusedRender.app + a distributable DMG (SPEC §12, DM-1/DM-4/DM-6).
#
# Pipeline: download a pinned standalone CPython -> unpack it into the .app's
# Resources -> pip-install this repo (with [bundled,app]) onto it -> write the
# launcher shim + Info.plist -> ad-hoc codesign -> hdiutil into a DMG.
#
# The bundled interpreter is a REAL python (python-build-standalone
# "install_only" build), not a PyInstaller freeze — the app runs
# `python3 -m fused_render.app` on it unchanged, and the subprocess executor
# (`sys.executable`) keeps working inside the bundle (DM-1).
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="FusedRender"
BUNDLE_ID="io.fused.render"

# Pinned python-build-standalone release (aarch64/arm64 macOS only for now —
# this script targets Apple Silicon; an x86_64-apple-darwin leg is a
# copy-paste-and-swap-the-triple follow-up, not built here).
PBS_RELEASE="20260623"
PBS_PYTHON_VERSION="3.12.13"
PBS_ASSET="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ASSET}"
# Verified against that release's SHA256SUMS manifest at build-script-authoring time.
PBS_SHA256="3724aa4dafb5f7b6c2cf98e89914e4248dc6bd2fe40407df4a2d73de99615f16"

VERSION="$(python3 -c "
import re
text = open('${REPO_ROOT}/pyproject.toml').read()
print(re.search(r'(?m)^version\s*=\s*\"([^\"]+)\"', text).group(1))
")"

BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"
CACHE_DIR="$BUILD_DIR/_cache"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
DMG_PATH="$DIST_DIR/${APP_NAME}-${VERSION}.dmg"

echo "==> fused-render ${VERSION} -> ${APP_NAME}.app -> ${DMG_PATH##*/}"

# ---------------------------------------------------------------------------
# 1. Fresh build tree
# ---------------------------------------------------------------------------

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources" "$CACHE_DIR" "$DIST_DIR"

# ---------------------------------------------------------------------------
# 2. Fetch + verify the standalone CPython, unpack into Contents/Resources/python
# ---------------------------------------------------------------------------

TARBALL="$CACHE_DIR/$PBS_ASSET"
if [[ ! -f "$TARBALL" ]]; then
  echo "==> downloading $PBS_ASSET"
  curl -fSL --retry 3 -o "$TARBALL" "$PBS_URL"
fi

echo "==> verifying sha256"
ACTUAL_SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$PBS_SHA256" ]]; then
  echo "FATAL: sha256 mismatch for $PBS_ASSET" >&2
  echo "  expected: $PBS_SHA256" >&2
  echo "  actual:   $ACTUAL_SHA256" >&2
  exit 1
fi

echo "==> unpacking standalone CPython"
tar -xzf "$TARBALL" -C "$APP_DIR/Contents/Resources/"
# Tarball's top-level dir is literally "python/", landing exactly at
# Contents/Resources/python/ with no extra mv needed.
BUNDLED_PYTHON="$APP_DIR/Contents/Resources/python/bin/python3"
test -x "$BUNDLED_PYTHON"

# ---------------------------------------------------------------------------
# 3. Install fused-render (+ its bundled data stack + the rumps app shell)
#    onto the bundled interpreter. Regular install, not editable — the
#    shipped app must not depend on the dev checkout's path.
# ---------------------------------------------------------------------------

echo "==> installing fused-render[bundled,app] onto the bundled interpreter"
"$BUNDLED_PYTHON" -m pip install --quiet --upgrade pip
"$BUNDLED_PYTHON" -m pip install --quiet "${REPO_ROOT}[bundled,app]"

# ---------------------------------------------------------------------------
# 4. Launcher shim
# ---------------------------------------------------------------------------

# "FusedRender" symlink to the interpreter: executing through it sets the
# process name (p_comm) to FusedRender, so Activity Monitor shows the app by
# its real name instead of "python3". Same binary — executor/sys.executable
# behavior unchanged.
ln -sfn python3 "$APP_DIR/Contents/Resources/python/bin/${APP_NAME}"

cat > "$APP_DIR/Contents/MacOS/${APP_NAME}" <<'SHIM'
#!/usr/bin/env bash
# Launches the bundled interpreter's menu-bar entry point. Resolved relative
# to this script's own location so the .app is relocatable (Applications,
# a DMG mount point, anywhere). Runs via the "FusedRender" interpreter
# symlink so the process is identifiable in Activity Monitor.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOURCES="$HERE/../Resources"
exec "$RESOURCES/python/bin/FusedRender" -m fused_render.app
SHIM
chmod +x "$APP_DIR/Contents/MacOS/${APP_NAME}"

# ---------------------------------------------------------------------------
# 5. Info.plist
# ---------------------------------------------------------------------------

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# ---------------------------------------------------------------------------
# 6. Ad-hoc codesign (DM-4). No Developer ID yet — testers right-click->Open
#    once. Real signing + notarization is a future hook, not built here:
# ---------------------------------------------------------------------------

echo "==> ad-hoc codesigning"
codesign --force --deep -s - "$APP_DIR"

# --- future hook: Developer ID signing + notarization -----------------------
# codesign --force --deep --options runtime \
#   -s "Developer ID Application: Your Name (TEAMID)" "$APP_DIR"
# xcrun notarytool submit "$DMG_PATH" --keychain-profile "fused-render" --wait
# xcrun stapler staple "$APP_DIR"
# -----------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. Assemble the DMG: app + Applications symlink, compressed UDZO
# ---------------------------------------------------------------------------

echo "==> building dmg"
STAGING_DIR="$BUILD_DIR/dmg-staging"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
cp -R "$APP_DIR" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

rm -f "$DMG_PATH"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov -format UDZO \
  "$DMG_PATH"

echo "==> done: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
