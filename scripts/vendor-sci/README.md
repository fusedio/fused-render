# Scientific-library vendor build

`geotiff_template.html`, `netcdf_template.html`, and `zarr_template.html` each
import a single self-contained ESM bundle so the product stays fully local at
runtime — no CDN, no module loader in the browser:

- `fused_render/templates/vendor/geotiff.bundle.mjs`  (geotiff 3.0.5)
- `fused_render/templates/vendor/netcdfjs.bundle.mjs` (netcdfjs 4.0.0)
- `fused_render/templates/vendor/zarrita.bundle.mjs`  (zarrita 0.7.3)
- `fused_render/templates/vendor/pdfjs.bundle.mjs` + `pdfjs.worker.bundle.mjs` (pdfjs-dist 4.10.38)

This directory is the build workspace that produces those bundles. Only the built
`*.bundle.mjs` files are committed; `node_modules/` here is gitignored.

## Regenerate

Node 22 is required (on the dev machine:
`/Users/akshilthumar/.nvm/versions/node/v22.17.1/bin`):

```sh
PATH="/Users/akshilthumar/.nvm/versions/node/v22.17.1/bin:$PATH" ./build.sh
```

`build.sh` runs `npm install` then esbuild once per library, emitting an ESM
bundle (`--format=esm --minify --bundle`, **no `--splitting`** so each library is
one file). Each `*-entry.mjs` names exactly what the matching template imports —
`geotiff`/`zarrita` re-export the whole namespace (`import * as …`), `netcdfjs`
re-exports just `NetCDFReader`.

Two self-containment notes worth keeping in mind if a version bump breaks the
build:

- **geotiff** decoders load via dynamic `import()`. With `--splitting` off esbuild
  inlines them into the single bundle. Its worker pool source is an inline blob
  (via the `web-worker` package), not an `import.meta.url` asset — the template
  never instantiates a `Pool` anyway (main-thread `readRasters`).
- **pdf.js** requires its worker as a SEPARATE module (`GlobalWorkerOptions.workerSrc`)
  — hence two bundles. Without cMaps/standard-font assets (runtime-fetched in
  stock pdf.js, deliberately not vendored) exotic CJK encodings and
  non-embedded fonts fall back; ordinary PDFs render fine.
- **zarrita** pulls in `numcodecs` for the zstd/blosc/lz4 codecs (the v3 test
  store uses zstd). numcodecs inlines its WASM as base64, so nothing is fetched
  at runtime — that inlined WASM is why `zarrita.bundle.mjs` is ~1.4 MB.

Templates import by absolute URL, served from the `/template-assets/*` mount:

```js
import * as geotiff from "/template-assets/geotiff.bundle.mjs";
import { NetCDFReader } from "/template-assets/netcdfjs.bundle.mjs";
import * as zarr from "/template-assets/zarrita.bundle.mjs";
import * as pdfjs from "/template-assets/pdfjs.bundle.mjs";
```
