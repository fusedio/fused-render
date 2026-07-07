#!/usr/bin/env bash
# Regenerate the scientific-library bundles under fused_render/templates/vendor/
# from the *-entry.mjs files. Node 22 is expected on PATH; on the dev machine it
# lives at /Users/akshilthumar/.nvm/versions/node/v22.17.1/bin. See README.md.
#
# Each library is emitted as its OWN self-contained ESM bundle (no code splitting,
# so a single file per lib). geotiff's decoders load via dynamic import() which
# esbuild inlines when --splitting is off; numcodecs (pulled in by zarrita for
# zstd/blosc) inlines its WASM as base64 — so nothing fetches at runtime.
set -euo pipefail
cd "$(dirname "$0")"

npm install

VENDOR=../../fused_render/templates/vendor
esb=./node_modules/.bin/esbuild

build() {
  # $1 = entry file, $2 = output bundle name
  "$esb" --bundle --format=esm --minify "$1" --outfile="$VENDOR/$2"
  echo "built $VENDOR/$2"
}

build geotiff-entry.mjs  geotiff.bundle.mjs
build netcdfjs-entry.mjs netcdfjs.bundle.mjs
build zarrita-entry.mjs  zarrita.bundle.mjs
