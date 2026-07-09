#!/usr/bin/env bash
# Regenerate the 3D bundles under fused_render/templates/vendor/ from the
# *-entry.mjs files. Uses bun to install and esbuild to bundle. Each library is
# emitted as its OWN self-contained ESM bundle (no code splitting, so a single
# file per lib) so nothing fetches at runtime — same offline rule as
# scripts/vendor-sci/. Only the built bundles are committed, not node_modules.
set -euo pipefail
cd "$(dirname "$0")"

bun install

VENDOR=../../fused_render/templates/vendor
esb=./node_modules/.bin/esbuild

build() {
  # $1 = entry file, $2 = output bundle name, $3+ = extra esbuild flags
  local entry="$1" out="$2"; shift 2
  "$esb" --bundle --format=esm --minify "$entry" --outfile="$VENDOR/$out" "$@"
  echo "built $VENDOR/$out"
}

build three-entry.mjs          three.bundle.mjs
# @gltf-transform/core lazily import()s node:fs/node:path in its NodeIO path,
# which the browser never uses (we only touch WebIO/Document/VertexLayout).
# Mark them external so esbuild leaves the untriggered dynamic import in place
# instead of failing to resolve a Node builtin.
build gltf-transform-entry.mjs gltf-transform.bundle.mjs --external:node:fs --external:node:path
