#!/usr/bin/env bash
# Build the apple tier's Swift helper (fused_render/ai/apple/helper/main.swift)
# into a single arm64 binary.
#
#   scripts/build_apple_helper.sh [OUT]
#
# OUT defaults to fused_render/ai/apple/bin/fused-apple-ai — the checkout
# location `fused_render/ai/apple/host.py` looks in (gitignored). build_dmg.sh
# and the `apple-helper` CI job pass their own OUT.
#
# Needs a full Xcode 26 (the FoundationModels and Speech frameworks live in the
# macOS 26 SDK; Command Line Tools alone do not ship them). Fails loudly when
# the selected toolchain is older, naming the fix, so a CI runner on the wrong
# image cannot produce a silently broken helper.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/fused_render/ai/apple/helper/main.swift"
OUT="${1:-$REPO_ROOT/fused_render/ai/apple/bin/fused-apple-ai}"
MIN_SDK_MAJOR=26

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FATAL: the Apple helper only builds on macOS" >&2
  exit 1
fi
if ! command -v swiftc >/dev/null 2>&1; then
  echo "FATAL: swiftc not found — install Xcode ${MIN_SDK_MAJOR} and run \`xcode-select -s /Applications/Xcode.app\`" >&2
  exit 1
fi
SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version 2>/dev/null || echo 0)"
SDK_MAJOR="${SDK_VERSION%%.*}"
if [[ "${SDK_MAJOR:-0}" -lt "$MIN_SDK_MAJOR" ]]; then
  echo "FATAL: the selected macOS SDK is ${SDK_VERSION}; the helper needs ${MIN_SDK_MAJOR}+" >&2
  echo "       (xcode-select -p -> $(xcode-select -p 2>/dev/null || echo '?'))" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
echo "==> building fused-apple-ai (SDK ${SDK_VERSION}) -> ${OUT}"
swiftc -O \
  -target "arm64-apple-macos${MIN_SDK_MAJOR}.0" \
  -framework FoundationModels -framework Speech -framework AVFoundation \
  -o "$OUT" "$SRC"
chmod +x "$OUT"
# The binary must at least start and answer a probe on the build host; a
# runner without Apple Intelligence still answers (available:false), which is
# all this checks.
PROBE_OUT="$("$OUT" probe </dev/null 2>/dev/null | head -1 || true)"
if [[ "$PROBE_OUT" != *'"type":"probe"'* ]]; then
  echo "FATAL: the built helper did not answer a probe:" >&2
  echo "$PROBE_OUT" >&2
  exit 1
fi
echo "    ok: $(echo "$PROBE_OUT" | cut -c1-120)"
