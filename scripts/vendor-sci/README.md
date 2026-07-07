# Scientific-library vendor build

`geotiff_template.html` and `netcdf_template.html` each import a single
self-contained ESM bundle so the product stays fully local at runtime — no CDN,
no module loader in the browser:

- `fused_render/templates/vendor/geotiff.bundle.mjs`  (geotiff 3.0.5)
- `fused_render/templates/vendor/netcdfjs.bundle.mjs` (netcdfjs 4.0.0)

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
`geotiff` re-exports the whole namespace (`import * as …`), `netcdfjs`
re-exports just `NetCDFReader`.

One self-containment note worth keeping in mind if a version bump breaks the
build:

- **geotiff** decoders load via dynamic `import()`. With `--splitting` off esbuild
  inlines them into the single bundle. Its worker pool source is an inline blob
  (via the `web-worker` package), not an `import.meta.url` asset — the template
  never instantiates a `Pool` anyway (main-thread `readRasters`).

Templates import by absolute URL, served from the `/template-assets/*` mount:

```js
import * as geotiff from "/template-assets/geotiff.bundle.mjs";
import { NetCDFReader } from "/template-assets/netcdfjs.bundle.mjs";
```
