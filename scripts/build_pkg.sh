#!/usr/bin/env bash
# Build the macOS network installer: a tiny, signed, notarized .pkg whose
# postinstall downloads this release's DMG from the CDN, verifies the sha256
# pinned here at build time, and copies the app into /Applications.
#
#   bash scripts/build_pkg.sh <stapled-dmg> <public-dmg-url>
#
# e.g. bash scripts/build_pkg.sh dist/FusedRender-0.6.84.dmg \
#          https://d2ic19jpchjovp.cloudfront.net/fused-render-dmgs/FusedRender-0.6.84.dmg
#
# Runs AFTER build_dmg.sh has produced the final DMG (signed, notarized AND
# stapled — stapling rewrites the file, so the hash must be taken from the
# bytes that will be uploaded) and before the DMG is uploaded; the URL is the
# one the upload step will publish it under. Output:
#   dist/<AppName>-<version>[-x86_64].pkg
# named from the DMG, so the pkg sits beside the DMG it installs.
#
# Pieces (scripts/pkg/):
#   postinstall       the install itself, static; reviewed in the repo
#   release.env       (generated) APP_NAME/BUNDLE_NAME/VERSION/ARCH/DMG_URL/DMG_SHA256
#   distribution.xml  Installer product: title, min OS, arch check, pages
#   welcome.html, conclusion.html   the two pages of the wizard
#
# Signing is credential-driven like build_dmg.sh (docs/signing.md): with a
# "Developer ID Installer" identity in the keychain (NOT the "Developer ID
# Application" one the app and DMG use — a different certificate type that
# only the Apple account holder can create) the pkg is signed, and with a
# notary profile also notarized and stapled. With no identity the pkg is
# built UNSIGNED for local inspection and the script says so: Gatekeeper
# refuses an unsigned pkg from the internet, so release.yml publishes the
# pkg only when `pkgutil --check-signature` says it is signed.
#
#   FUSED_RENDER_INSTALLER_IDENTITY  "Developer ID Installer: Name (TEAMID)"
#                                    or its SHA-1; auto-detected when exactly
#                                    one such identity is in the keychain
#   FUSED_RENDER_CODESIGN_KEYCHAIN   optional keychain to search / sign from
#   FUSED_RENDER_NOTARY_PROFILE      optional notarytool keychain profile
set -euo pipefail

on_err() {
  echo "FATAL: build_pkg.sh failed at line $1: $2" >&2
}
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR

if [[ $# -ne 2 ]]; then
  echo "usage: bash scripts/build_pkg.sh <stapled-dmg> <public-dmg-url>" >&2
  exit 2
fi
DMG_PATH="$1"
DMG_URL="$2"
[[ -f "$DMG_PATH" ]] || { echo "FATAL: no such DMG: $DMG_PATH" >&2; exit 1; }
case "$DMG_URL" in
  https://*) ;;
  *) echo "FATAL: the DMG URL must be https:// (got: $DMG_URL)" >&2; exit 1 ;;
esac
if [[ "$(basename "$DMG_URL")" != "$(basename "$DMG_PATH")" ]]; then
  echo "FATAL: the URL names $(basename "$DMG_URL") but the file is $(basename "$DMG_PATH") —" >&2
  echo "       the pkg would pin one file's hash and download another." >&2
  exit 1
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FATAL: pkgbuild/productbuild are macOS tools; run this on a Mac." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_SRC="$REPO_ROOT/scripts/pkg"

# <AppName>[-<branch-suffix>]-<version>[-x86_64].dmg -> name, version, arch.
# The app name part is the flavor's app_name plus an optional lowercase
# branch suffix (fused_render/_branch.py), the version always starts with a
# digit; the greedy match takes the LAST digit-led segment as the version so
# a branch suffix like `123-fix` cannot be mistaken for one.
DMG_BASENAME="$(basename "$DMG_PATH")"
if [[ "$DMG_BASENAME" =~ ^(.+)-([0-9][^-]*)(-x86_64)?\.dmg$ ]]; then
  APP_NAME="${BASH_REMATCH[1]}"
  VERSION="${BASH_REMATCH[2]}"
  ARCH="arm64"
  ARCH_SUFFIX=""
  if [[ -n "${BASH_REMATCH[3]:-}" ]]; then
    ARCH="x86_64"
    ARCH_SUFFIX="-x86_64"
  fi
else
  echo "FATAL: cannot parse app name/version from $DMG_BASENAME (expected <App>-<version>[-x86_64].dmg)" >&2
  exit 1
fi
APP_BASE="${APP_NAME%%-*}"   # FusedRender / FusedBot, branch suffix dropped
BRANCH_SUFFIX="${APP_NAME#"$APP_BASE"}"   # "" or "-<ref>"

# Bundle id and display name from the flavor table, keyed by the app name the
# DMG carries (no FUSED_RENDER_FLAVOR guesswork: the DMG already says which
# app it is). The branch suffix goes into the receipt id the way it goes into
# CFBundleIdentifier (setup_py2app.py), so a feature build's receipt never
# collides with the release's.
# Tab-separated: the display and bundle names hold spaces ("Fused Render").
IFS=$'\t' read -r BUNDLE_ID DISPLAY_NAME BUNDLE_BASE < <(PYTHONPATH="$REPO_ROOT" python3 -c "
from fused_render import _flavor
ids = {v['app_name']: (v['bundle_id'], v['display_name'], v['bundle_name']) for v in _flavor._IDENTITY.values()}
print('\t'.join(ids['$APP_BASE']))
")
if [[ -z "${BUNDLE_BASE:-}" ]]; then
  echo "FATAL: no flavor in fused_render/_flavor.py has app_name $APP_BASE" >&2
  exit 1
fi
# The .app folder inside the DMG ("Fused Render[-<ref>].app"), which is not
# the DMG's file-name token (APP_NAME, "FusedRender[-<ref>]").
BUNDLE_NAME="${BUNDLE_BASE}${BRANCH_SUFFIX}"
LEGACY_BUNDLE_NAMES="$(PYTHONPATH="$REPO_ROOT" python3 -c "
from fused_render import _flavor
ids = {v['app_name']: v['legacy_bundle_names'] for v in _flavor._IDENTITY.values()}
print(' '.join(n + '$BRANCH_SUFFIX' for n in ids['$APP_BASE']))
")"
BRANCH_REF="${BRANCH_SUFFIX#-}"
PKG_ID="${BUNDLE_ID}${BRANCH_REF:+.$BRANCH_REF}.installer"

DMG_SHA256="$(shasum -a 256 "$DMG_PATH" | cut -d' ' -f1)"
DMG_BYTES="$(stat -f %z "$DMG_PATH")"
DMG_SIZE_MB="$(( (DMG_BYTES + 524288) / 1048576 ))"
# What Installer's "Installation Type" page reports as the space taken: the
# installed app's size when the bundle build_dmg.sh just produced is still
# around (it is, in the release job), else the compressed DMG's as a floor.
APP_BUILD_DIR="$REPO_ROOT/build/py2app-dist/${BUNDLE_NAME}.app"
if [[ -d "$APP_BUILD_DIR" ]]; then
  INSTALL_KBYTES="$(du -sk "$APP_BUILD_DIR" | cut -f1)"
else
  INSTALL_KBYTES="$(( $(stat -f %z "$DMG_PATH") / 1024 ))"
fi

BUILD_DIR="$REPO_ROOT/build/pkg${ARCH_SUFFIX}"
DIST_DIR="$REPO_ROOT/dist"
PKG_PATH="$DIST_DIR/${APP_NAME}-${VERSION}${ARCH_SUFFIX}.pkg"
COMPONENT_PKG="${APP_NAME}-component.pkg"

echo "==> ${APP_NAME} ${VERSION} [${ARCH}] installer -> ${PKG_PATH##*/}"
echo "    installs $DMG_URL"
echo "    sha256   $DMG_SHA256 (${DMG_SIZE_MB} MB)"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/scripts" "$BUILD_DIR/resources" "$DIST_DIR"

# ---------------------------------------------------------------------------
# 1. Scripts dir: the static postinstall + this release's facts
# ---------------------------------------------------------------------------
cp "$PKG_SRC/postinstall" "$BUILD_DIR/scripts/postinstall"
chmod 755 "$BUILD_DIR/scripts/postinstall"
bash -n "$BUILD_DIR/scripts/postinstall"
# Single-quoted values: the URL and names are ours, but a shell-sourced file
# must never be able to execute a surprise.
cat > "$BUILD_DIR/scripts/release.env" <<EOF
# Generated by build_pkg.sh for one release; read by postinstall.
APP_NAME='${APP_NAME}'
BUNDLE_NAME='${BUNDLE_NAME}'
LEGACY_BUNDLE_NAMES='${LEGACY_BUNDLE_NAMES}'
DISPLAY_NAME='${DISPLAY_NAME}'
VERSION='${VERSION}'
ARCH='${ARCH}'
DMG_URL='${DMG_URL}'
DMG_SHA256='${DMG_SHA256}'
EOF
chmod 644 "$BUILD_DIR/scripts/release.env"
bash -n "$BUILD_DIR/scripts/release.env"

# ---------------------------------------------------------------------------
# 2. Installer pages + product definition from the templates
# ---------------------------------------------------------------------------
fill() {  # fill <template> <output>: substitute every @PLACEHOLDER@
  sed \
    -e "s|@APP_NAME@|${APP_NAME}|g" \
    -e "s|@DISPLAY_NAME@|${DISPLAY_NAME}|g" \
    -e "s|@VERSION@|${VERSION}|g" \
    -e "s|@ARCH@|${ARCH}|g" \
    -e "s|@PKG_ID@|${PKG_ID}|g" \
    -e "s|@COMPONENT_PKG@|${COMPONENT_PKG}|g" \
    -e "s|@DMG_SIZE_MB@|${DMG_SIZE_MB}|g" \
    -e "s|@INSTALL_KBYTES@|${INSTALL_KBYTES}|g" \
    "$1" > "$2"
  if grep -q '@[A-Z0-9_]*@' "$2"; then
    echo "FATAL: unfilled placeholder in $2: $(grep -o '@[A-Z0-9_]*@' "$2" | sort -u | tr '\n' ' ')" >&2
    exit 1
  fi
}
fill "$PKG_SRC/welcome.html" "$BUILD_DIR/resources/welcome.html"
fill "$PKG_SRC/conclusion.html" "$BUILD_DIR/resources/conclusion.html"
fill "$PKG_SRC/distribution.xml" "$BUILD_DIR/distribution.xml"
xmllint --noout "$BUILD_DIR/distribution.xml"

# ---------------------------------------------------------------------------
# 3. Component package: scripts only, no payload
# ---------------------------------------------------------------------------
# --nopayload: nothing is laid down by Installer itself, so its bundle
# "relocation" (which can put a payload app anywhere an older copy lives)
# has nothing to act on; the postinstall decides the path. Installer writes
# no receipt for a payload-free component (`pkgutil --pkgs` never lists it),
# so there is nothing to forget: the app is removed by dragging it to the
# Trash, as with the DMG.
echo "==> pkgbuild (component, no payload)"
pkgbuild --nopayload \
  --scripts "$BUILD_DIR/scripts" \
  --identifier "$PKG_ID" \
  --version "$VERSION" \
  "$BUILD_DIR/$COMPONENT_PKG"

# ---------------------------------------------------------------------------
# 4. Product archive, signed when an installer identity is available
# ---------------------------------------------------------------------------
# A scalar, not an array: macOS's /bin/bash is 3.2, where an empty array
# trips `set -u`. Same rule as build_dmg.sh: the keychain path has no spaces.
KC_PATH="${FUSED_RENDER_CODESIGN_KEYCHAIN:-}"
KC_OPT=""
if [[ -n "$KC_PATH" ]]; then
  KC_OPT="--keychain $KC_PATH"
fi
SIGN_IDENTITY=""
if [[ -n "${FUSED_RENDER_INSTALLER_IDENTITY:-}" ]]; then
  SIGN_IDENTITY="$FUSED_RENDER_INSTALLER_IDENTITY"
else
  # No `-p codesigning`: that policy filter hides installer identities.
  IDENTITY_LINES="$(security find-identity -v $KC_PATH 2>/dev/null \
    | grep 'Developer ID Installer' || true)"
  IDENTITY_COUNT="$(printf '%s' "$IDENTITY_LINES" | grep -c 'Developer ID Installer' || true)"
  if [[ "$IDENTITY_COUNT" == "1" ]]; then
    SIGN_IDENTITY="$(printf '%s' "$IDENTITY_LINES" | sed -E 's/.*"(.*)".*/\1/')"
  elif [[ "$IDENTITY_COUNT" -gt 1 ]]; then
    echo "FATAL: multiple 'Developer ID Installer' identities in the keychain." >&2
    echo "       Set FUSED_RENDER_INSTALLER_IDENTITY to pick one of:" >&2
    printf '%s\n' "$IDENTITY_LINES" >&2
    exit 1
  fi
fi

# productbuild regenerates every pkg-ref's installKBytes from the component,
# which is 0 for a payload-free one, so the "Installation Type" page would say
# the install takes Zero KB whatever the template says. Build unsigned, put the
# real figure back into the Distribution, re-flatten, and sign the result with
# productsign (signing must come last: it covers the final Distribution).
UNSIGNED_PKG="$BUILD_DIR/product-unsigned.pkg"
EXPANDED="$BUILD_DIR/product-expanded"
PATCHED_PKG="$BUILD_DIR/product-patched.pkg"
rm -rf "$UNSIGNED_PKG" "$EXPANDED" "$PATCHED_PKG" "$PKG_PATH"
echo "==> productbuild"
productbuild \
  --distribution "$BUILD_DIR/distribution.xml" \
  --resources "$BUILD_DIR/resources" \
  --package-path "$BUILD_DIR" \
  --version "$VERSION" \
  "$UNSIGNED_PKG"
pkgutil --expand "$UNSIGNED_PKG" "$EXPANDED"
sed -i '' -E "s/installKBytes=\"[0-9]+\"/installKBytes=\"${INSTALL_KBYTES}\"/" "$EXPANDED/Distribution"
grep -q "installKBytes=\"${INSTALL_KBYTES}\"" "$EXPANDED/Distribution" || {
  echo "FATAL: could not set installKBytes in the product Distribution" >&2
  exit 1
}
pkgutil --flatten "$EXPANDED" "$PATCHED_PKG"

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "==> productsign: $SIGN_IDENTITY"
  productsign \
    --sign "$SIGN_IDENTITY" --timestamp $KC_OPT \
    "$PATCHED_PKG" "$PKG_PATH"
  pkgutil --check-signature "$PKG_PATH"
else
  echo "==> UNSIGNED (no 'Developer ID Installer' identity; local inspection only)"
  echo "    Gatekeeper refuses an unsigned pkg from the internet — see docs/signing.md"
  mv "$PATCHED_PKG" "$PKG_PATH"
fi

# ---------------------------------------------------------------------------
# 5. Notarize + staple (the pkg is its own outermost container)
# ---------------------------------------------------------------------------
if [[ -n "${FUSED_RENDER_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "$SIGN_IDENTITY" ]]; then
    # Not fatal, unlike the DMG: the installer certificate may not exist yet
    # while the notary profile does (CI sets it for the DMG), and a release
    # must not fail over the optional pkg. The pkg is simply not publishable.
    echo "==> skipping notarization: the pkg is unsigned (notarization needs a Developer ID Installer signature)"
  else
    echo "==> notarizing ${PKG_PATH##*/} (profile: $FUSED_RENDER_NOTARY_PROFILE)"
    xcrun notarytool submit "$PKG_PATH" \
      --keychain-profile "$FUSED_RENDER_NOTARY_PROFILE" --wait
    echo "==> stapling notarization ticket"
    xcrun stapler staple "$PKG_PATH"
    xcrun stapler validate "$PKG_PATH"
  fi
else
  echo "==> skipping notarization (FUSED_RENDER_NOTARY_PROFILE unset)"
fi

echo "==> done: $PKG_PATH ($(( ($(stat -f %z "$PKG_PATH") + 512) / 1024 )) KB)"
if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "    signed; install with: open '$PKG_PATH'  (or: sudo installer -pkg '$PKG_PATH' -target /)"
else
  echo "    unsigned; to try it locally: sudo installer -pkg '$PKG_PATH' -target /"
fi
