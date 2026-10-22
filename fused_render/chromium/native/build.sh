#!/usr/bin/env bash
# Build the Chromium engine for one architecture:
#
#   fused_render/chromium/native/build.sh [arm64|x86_64] [OUT_DIR]
#
# 1. reads `cef_version` / `cef_sha1` from ../manifest.json,
# 2. fetches that CEF binary distribution (minimal) from cef-builds.spotifycdn.com,
# 3. builds FusedChromium.framework and the helper with CMake,
# 4. assembles OUT_DIR/FusedChromium/ — the framework, the CEF framework,
#    the five helper app bundles CEF expects, engine.json — and ad-hoc signs
#    it so it runs locally. scripts/build_dmg.sh (step 4e-bis) runs this, seats
#    the output in the app bundle and signs it with the Developer ID (D1415);
#    a dev checkout points FUSED_RENDER_CHROMIUM_ENGINE_DIR at the output.
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

read -r CEF_VERSION CEF_SHA1 < <(python3 - "$HERE/../manifest.json" "$ARCH" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
print(m["cef_version"], (m.get("cef_sha1") or {}).get(sys.argv[2]) or "-")
PYEOF
)
if [[ "$CEF_SHA1" == "-" ]]; then
  echo "FATAL: manifest.json has no cef_sha1 for $ARCH (the CDN publishes it beside the tarball)" >&2
  exit 1
fi
# The engine's version is the app's: one build, one version.
ENGINE_VERSION="$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' "$HERE/../../__init__.py")"
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

echo "==> engine ${ENGINE_VERSION:-dev} / CEF $CEF_VERSION / $ARCH"
# The tarball is verified against the manifest's sha1 on EVERY run (a CI
# cache is keyed by manifest, but a bad entry would otherwise be reused), and
# the extracted tree counts only with a marker naming the verified digest:
# an extraction that died halfway is redone, into a temp dir renamed into
# place.
TARBALL="$CACHE/$DIST.tar.bz2"
if [[ ! -f "$TARBALL" ]]; then
  echo "==> fetching $DIST_URL"
  # To a temp name, renamed only when curl succeeds: a partial file under
  # the final name would skip the fetch next time and fail the checksum.
  rm -f "$TARBALL.part"
  curl -fL --retry 3 -o "$TARBALL.part" "$DIST_URL"
  mv "$TARBALL.part" "$TARBALL"
fi
ACTUAL_SHA1="$(shasum -a 1 "$TARBALL" | cut -d' ' -f1)"
if [[ "$ACTUAL_SHA1" != "$CEF_SHA1" ]]; then
  rm -f "$TARBALL"
  echo "FATAL: $DIST.tar.bz2 sha1 $ACTUAL_SHA1 != manifest $CEF_SHA1 (removed; rerun to refetch)" >&2
  exit 1
fi
MARKER="$CACHE/$DIST/.verified-sha1"
if [[ ! -f "$MARKER" || "$(cat "$MARKER")" != "$CEF_SHA1" ]]; then
  echo "==> extracting $DIST"
  rm -rf "$CACHE/$DIST" "$CACHE/.extract-$DIST"
  mkdir -p "$CACHE/.extract-$DIST"
  tar -xjf "$TARBALL" -C "$CACHE/.extract-$DIST"
  mv "$CACHE/.extract-$DIST/$DIST" "$CACHE/$DIST"
  rmdir "$CACHE/.extract-$DIST"
  printf '%s' "$CEF_SHA1" > "$MARKER"
fi
CEF_ROOT="$CACHE/$DIST"

echo "==> cmake"
cmake -S "$HERE" -B "$BUILD" \
  -DCEF_ROOT="$CEF_ROOT" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$ARCH" \
  -DFR_ENGINE_VERSION="${ENGINE_VERSION:-0}" \
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
cat > "$STAGE/engine.json" <<EOF2
{"cef_version": "$CEF_VERSION", "arch": "$ARCH", "app_version": "${ENGINE_VERSION:-}"}
EOF2

# ---- sign (ad hoc) --------------------------------------------------------------------
# Enough to run from a dev checkout (the app's hardened runtime disables
# library validation, and on Apple silicon an ad-hoc signature runs). The
# release signature — Developer ID, hardened runtime, timestamp, Chromium's
# helper entitlements — is build_dmg.sh step 5's, applied inside out over
# the whole engine once it sits in the bundle.
echo "==> signing (ad hoc)"
while IFS= read -r -d '' lib; do codesign --force -s - "$lib"; done < <(
  find "$CEF_FW/Versions/A/Libraries" -type f -name '*.dylib' -print0 2>/dev/null)
codesign --force -s - "$CEF_FW"
codesign --force -s - "$STAGE/FusedChromium.framework"
for app in "$STAGE"/*.app; do codesign --force -s - "$app"; done
codesign --verify --strict "$STAGE/FusedChromium.framework"
echo "==> $STAGE"
