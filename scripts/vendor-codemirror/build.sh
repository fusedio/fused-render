#!/usr/bin/env bash
# Regenerate fused_render/templates/vendor/codemirror.bundle.js from entry.js.
# Node 22 is expected on PATH; on the dev machine it lives at
# /opt/homebrew/opt/node@22/bin. See README.md.
set -euo pipefail
cd "$(dirname "$0")"

npm install
./node_modules/.bin/esbuild \
  --bundle \
  --format=iife \
  --global-name=CM \
  --minify \
  entry.js \
  --outfile=../../fused_render/templates/vendor/codemirror.bundle.js

echo "built ../../fused_render/templates/vendor/codemirror.bundle.js"
