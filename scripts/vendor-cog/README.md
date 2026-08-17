# vendor-cog

Build workspace that bundles the browser Cloud-Optimized GeoTIFF reader used by
the `map` viewer template, committed under `fused_render/templates/vendor/`:

- `cog.bundle.mjs` — `COGLayer` (`@developmentseed/deck.gl-geotiff`) plus the
  deck.gl surface the map viewer uses (`MapboxOverlay`, `BitmapLayer`,
  `GeoJsonLayer`, `ScatterplotLayer`), `DecoderPool`, and `parseWkt`, all
  re-exported by `cog-entry.mjs`.
- `cog.worker.bundle.mjs` — the tile decoder `DecoderPool` runs off the main
  thread, bundled from the package's own worker entry.

deck.gl has to come from this same bundle. Two copies of luma.gl in one page
fail with *"This version of luma.gl has already been initialized"*, so the map
viewer must not also load deck.gl separately.

## Why the map viewer reads COGs in the browser

A remote COG served through the Python tile daemon costs a describe plus one
HTTP round trip per 256px tile, and every tile URL embeds that daemon's
ephemeral port — so the map breaks whenever the daemon is replaced. Reading the
COG directly means first paint in ~2.4s against ~3.7-6.3s, no blank map while
tiles arrive (the coarse overview stays painted and refines in place), and no
requests at all when panning back over ground already fetched. Local files,
non-TIFF drivers, and categorical/colormap rendering stay on the Python engine —
`COGLayer` renders strictly what the TIFF tags say.

## Rebuild

```sh
./build.sh
```

Needs `bun` on PATH. Installs the pinned deps and runs esbuild. Only the built
bundles are committed — `node_modules/` is git-ignored. Versions are pinned in
`package.json`; deck.gl must stay in lockstep between `deck.gl` and
`@deck.gl/mapbox`.

Two things the build has to work around, both load-bearing:

- `--alias:module=./node-stub.mjs` — a dependency imports Node's `module`
  behind a runtime environment check that is false in a browser, but esbuild
  still has to resolve it.
- The worker is bundled from its package file directly rather than through a
  wrapper entry. Its package declares `"sideEffects": false` while the module's
  entire job *is* a side effect (registering an `onmessage` handler), so esbuild
  drops it when it is merely imported — and honours that over
  `--tree-shaking=false`. An entry point is never dropped.

The template loads these from `/template-assets/` (mapped to
`fused_render/templates/vendor/`), never from a CDN — same offline
self-containment rule as `scripts/vendor-three/`.
