#!/usr/bin/env bash
# Regenerate fused_render/templates/vendor/map.bundle.mjs (+ map.bundle.css)
# from map-entry.mjs. bun installs the pinned deps, esbuild bundles. One
# self-contained ESM file, no code splitting. Only the built bundle is
# committed, never node_modules/. See README.md.
set -euo pipefail
cd "$(dirname "$0")"

bun install

VENDOR=../../fused_render/templates/vendor
./node_modules/.bin/esbuild map-entry.mjs --bundle --format=esm --minify \
  --platform=browser --alias:module=./node-stub.mjs --loader:.png=dataurl \
  --external:cog-tiler-wasm --external:react --external:react-dom \
  --outfile="$VENDOR/map.bundle.mjs"
# The GeoTIFF decoder worker. Bundled from its package file directly (an
# entry point is never tree-shaken; the package's "sideEffects": false would
# drop a merely-imported onmessage registration — an entry point is never dropped),
# then the pool's `new URL("./worker.js", import.meta.url)` is pointed at it:
# esbuild leaves that URL untouched, and a bare worker.js under
# /template-assets/ would be nobody's name.
./node_modules/.bin/esbuild node_modules/@developmentseed/geotiff/dist/pool/worker.js \
  --bundle --format=esm --minify --platform=browser --alias:module=./node-stub.mjs \
  --outfile="$VENDOR/map.worker.bundle.mjs"
grep -q 'new URL("./worker.js",import.meta.url)' "$VENDOR/map.bundle.mjs"
sed -i.bak 's#new URL("./worker.js",import.meta.url)#new URL("./map.worker.bundle.mjs",import.meta.url)#' "$VENDOR/map.bundle.mjs"
# The vector library tags its internal feature properties (KML icons) with an
# upstream-branded prefix. They are written and read only by that library, so
# renaming every occurrence consistently changes nothing but the name. The
# prefix is read off its KML icon key rather than spelled out here.
prefix=$(grep -o '"__[a-z]*_kml_icon_url"' "$VENDOR/map.bundle.mjs" | head -1 | sed 's/^"\(__[a-z]*_\)kml_icon_url"$/\1/')
if [ -n "$prefix" ] && [ "$prefix" != "__fmvec_" ]; then
  sed -i.bak "s#${prefix}#__fmvec_#g" "$VENDOR/map.bundle.mjs"
fi
# The point-cloud library loads its LAZ decoder from unpkg; ship it beside the
# bundle instead so LAZ/COPC opens offline (fails if the URL stops matching).
LAZ_CDN='"https://unpkg.com/laz-perf@0.0.7/lib/web/laz-perf.wasm"'
grep -qF "$LAZ_CDN" "$VENDOR/map.bundle.mjs"
sed -i.bak "s#${LAZ_CDN}#new URL(\"./laz-perf.wasm\",import.meta.url).href#g" "$VENDOR/map.bundle.mjs"
cp node_modules/copc/node_modules/laz-perf/lib/web/laz-perf.wasm "$VENDOR/laz-perf.wasm"
rm -f "$VENDOR/map.bundle.mjs.bak"
cat node_modules/maplibre-gl/dist/maplibre-gl.css node_modules/maplibre-gl-lidar/dist/maplibre-gl-lidar.css > "$VENDOR/map.bundle.css"
echo "built $VENDOR/map.bundle.mjs"
