#!/usr/bin/env bash
# Regenerate the COG/deck.gl bundles under fused_render/templates/vendor/ from
# the *-entry.mjs files. Uses bun to install and esbuild to bundle. Each entry is
# emitted as its OWN self-contained ESM bundle (no code splitting, so a single
# file per entry) so nothing fetches at runtime — same offline rule as
# scripts/vendor-three/. Only the built bundles are committed, not node_modules.
set -euo pipefail
cd "$(dirname "$0")"

bun install

VENDOR=../../fused_render/templates/vendor
esb=./node_modules/.bin/esbuild

build() {
  # $1 = entry file, $2 = output bundle name, $3+ = extra esbuild flags
  local entry="$1" out="$2"; shift 2
  # The colormap sprite inlines as a data URL rather than shipping as a second
  # asset the page has to locate and fetch; it is 16KB.
  "$esb" --bundle --format=esm --minify --platform=browser \
    --alias:module=./node-stub.mjs --loader:.png=dataurl \
    "$entry" --outfile="$VENDOR/$out" "$@"
  echo "built $VENDOR/$out"
}

build cog-entry.mjs        cog.bundle.mjs

# The decoder worker is bundled from its package file directly, not through a
# wrapper entry that imports it. Its package declares "sideEffects": false while
# the module's entire job IS a side effect (registering an onmessage handler),
# so esbuild drops it when it is merely imported — and honours that over
# --tree-shaking=false. An entry point is never dropped.
build node_modules/@developmentseed/geotiff/dist/pool/worker.js cog.worker.bundle.mjs
