#!/usr/bin/env bash
# Build the downloadable Chromium engine artifact for one architecture:
#
#   fused_render/chromium/native/build.sh [arm64|x86_64] [OUT_DIR]
#
# 1. reads `cef_version` / `engine_version` from ../manifest.json,
# 2. fetches that CEF binary distribution (minimal) from cef-builds.spotifycdn.com,
# 3. builds FusedChromium.framework and the helper with CMake,
# 4. assembles OUT_DIR/FusedChromium/ — the framework, the CEF framework,
#    the five helper app bundles CEF expects, engine.json —
# 5. signs it (FUSED_RENDER_CODESIGN_IDENTITY, else ad hoc) and zips it as
#    FusedChromium-<engine_version>-<arch>.zip next to a .json with its
#    sha256 and size (what scripts/publish_chromium_engine.py pins).
#
# Needs: macOS, Xcode command line tools, cmake ≥ 3.21, python3, curl.
# The CEF download is ~200 MB; FUSED_RENDER_CEF_CACHE keeps it between runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="${1:-$(uname -m)}"
OUT_DIR="${2:-$HERE/out}"
case "$ARCH" in
  arm64|x86_64) ;;
  *) echo "FATAL: arch must be arm64 or x86_64, got $ARCH" >&2; exit 1 ;;
esac
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FATAL: the engine only builds on macOS" >&2
  exit 1
fi

read -r CEF_VERSION ENGINE_VERSION < <(python3 - "$HERE/../manifest.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
print(m["cef_version"], m["engine_version"])
EOF
)
case "$ARCH" in
  arm64) CEF_PLATFORM="macosarm64" ;;
  x86_64) CEF_PLATFORM="macosx64" ;;
esac
DIST="cef_binary_${CEF_VERSION}_${CEF_PLATFORM}_minimal"
# `+` in the version is %2B in the CDN's URL.
DIST_URL="https://cef-builds.spotifycdn.com/$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$DIST.tar.bz2")"
CACHE="${FUSED_RENDER_CEF_CACHE:-$HERE/.cef-cache}"
BUILD="$HERE/build-$ARCH"
mkdir -p "$CACHE" "$BUILD" "$OUT_DIR"

echo "==> engine $ENGINE_VERSION / CEF $CEF_VERSION / $ARCH"
if [[ ! -d "$CACHE/$DIST" ]]; then
  echo "==> fetching $DIST_URL"
  curl -fL --retry 3 -o "$CACHE/$DIST.tar.bz2" "$DIST_URL"
  tar -xjf "$CACHE/$DIST.tar.bz2" -C "$CACHE"
fi
CEF_ROOT="$CACHE/$DIST"

echo "==> cmake"
cmake -S "$HERE" -B "$BUILD" \
  -DCEF_ROOT="$CEF_ROOT" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$ARCH" \
  -DFR_ENGINE_VERSION="$ENGINE_VERSION" \
  -DPROJECT_ARCH="$ARCH"
cmake --build "$BUILD" --config Release -j "$(sysctl -n hw.ncpu)"

# ---- assemble ---------------------------------------------------------------------
STAGE="$OUT_DIR/FusedChromium"
rm -rf "$STAGE"
mkdir -p "$STAGE"
FW_BUILT="$(find "$BUILD" -name 'FusedChromium.framework' -type d -maxdepth 3 | head -1)"
HELPER_BUILT="$(find "$BUILD" -name 'FusedRender Helper.app' -type d -maxdepth 3 | head -1)"
[[ -d "$FW_BUILT" ]] || { echo "FATAL: FusedChromium.framework not built" >&2; exit 1; }
[[ -d "$HELPER_BUILT" ]] || { echo "FATAL: FusedRender Helper.app not built" >&2; exit 1; }
cp -R "$FW_BUILT" "$STAGE/"
# The distribution ships the CEF framework FLAT (no Versions/); codesign on
# Xcode 26 wants the versioned layout, so it is rebuilt here the way CEF's
# own COPY_MAC_FRAMEWORK cmake macro does: everything under Versions/A,
# Current -> A, and top-level symlinks to the binary, Libraries, Resources.
CEF_FW="$STAGE/Chromium Embedded Framework.framework"
if [[ -d "$CEF_ROOT/Release/Chromium Embedded Framework.framework/Versions" ]]; then
  ditto "$CEF_ROOT/Release/Chromium Embedded Framework.framework" "$CEF_FW"  # versioned upstream
else
  mkdir -p "$CEF_FW/Versions/A"
  ditto "$CEF_ROOT/Release/Chromium Embedded Framework.framework" "$CEF_FW/Versions/A"
  (cd "$CEF_FW/Versions" && ln -s A Current)
  for entry in "Chromium Embedded Framework" Libraries Resources; do
    if [[ -e "$CEF_FW/Versions/A/$entry" ]]; then
      (cd "$CEF_FW" && ln -s "Versions/Current/$entry" "$entry")
    fi
  done
fi
# The helper and its four variants (GPU, Renderer, Plugin, Alerts): CEF finds
# them by name next to the main helper, each with its own bundle id suffix.
cp -R "$HELPER_BUILT" "$STAGE/FusedRender Helper.app"
for variant in GPU Renderer Plugin Alerts; do
  name="FusedRender Helper ($variant)"
  app="$STAGE/$name.app"
  cp -R "$HELPER_BUILT" "$app"
  mv "$app/Contents/MacOS/FusedRender Helper" "$app/Contents/MacOS/$name"
  suffix="$(echo "$variant" | tr '[:upper:]' '[:lower:]')"
  /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable $name" \
    -c "Set :CFBundleName $name" -c "Set :CFBundleDisplayName $name" \
    -c "Set :CFBundleIdentifier io.fused.render.chromium.helper.$suffix" \
    "$app/Contents/Info.plist"
done
cat > "$STAGE/engine.json" <<EOF
{"engine_version": "$ENGINE_VERSION", "cef_version": "$CEF_VERSION", "arch": "$ARCH"}
EOF

# ---- sign ------------------------------------------------------------------------------
# The engine is downloaded by the app, never quarantined, and the app's
# hardened runtime disables library validation (build_dmg.sh entitlements),
# so an ad-hoc signature is enough for a local build; a release signs with
# the Developer ID (hardened runtime + timestamp) so Gatekeeper has no say
# either way. The helpers run V8 (JIT) and load the CEF framework from
# outside their own bundle: Chromium's helper entitlements, verbatim.
IDENTITY="${FUSED_RENDER_CODESIGN_IDENTITY:--}"
ENTITLEMENTS="$BUILD/helper-entitlements.plist"
cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict>
</plist>
PLIST
if [[ "$IDENTITY" == "-" ]]; then
  sign() { codesign --force -s - "$@"; }
  sign_exe() { codesign --force -s - "$@"; }
else
  sign() { codesign --force --options runtime --timestamp -s "$IDENTITY" "$@"; }
  sign_exe() { codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" -s "$IDENTITY" "$@"; }
fi
echo "==> signing ($IDENTITY)"
# Inside out: the CEF framework's nested binaries, then the frameworks, then the helpers.
while IFS= read -r -d '' lib; do sign "$lib"; done < <(
  find "$CEF_FW/Versions/A/Libraries" -type f -name '*.dylib' -print0 2>/dev/null)
sign "$CEF_FW"
sign "$STAGE/FusedChromium.framework"
for app in "$STAGE"/*.app; do sign_exe "$app"; done
codesign --verify --strict "$STAGE/FusedChromium.framework"

# ---- zip ----------------------------------------------------------------------------------
ZIP="$OUT_DIR/FusedChromium-${ENGINE_VERSION}-${ARCH}.zip"
rm -f "$ZIP"
# -y keeps symlinks as symlinks (the Python unpacker recreates them).
(cd "$OUT_DIR" && zip -qry "$ZIP" FusedChromium)
SHA="$(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
SIZE="$(stat -f%z "$ZIP")"
cat > "${ZIP%.zip}.json" <<EOF
{"engine_version": "$ENGINE_VERSION", "cef_version": "$CEF_VERSION", "arch": "$ARCH", "file": "$(basename "$ZIP")", "sha256": "$SHA", "size": $SIZE}
EOF
echo "==> $ZIP"
echo "    sha256 $SHA"
echo "    size   $SIZE"
