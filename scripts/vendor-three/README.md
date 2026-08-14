# vendor-three

Build workspace that bundles the Three.js surface used by the read-only `glb`
viewer template into a self-contained ESM bundle committed under
`fused_render/templates/vendor/`:

- `three.bundle.mjs` — Three.js core + `OrbitControls` + `GLTFLoader`,
  re-exported by `three-entry.mjs`.

## Rebuild

```sh
./build.sh
```

Needs `bun` on PATH. Installs the pinned deps and runs esbuild. Only the built
`three.bundle.mjs` file is committed — `node_modules/` and `bun.lockb` are
git-ignored (see `.gitignore`). The version is pinned in `package.json`
(three 0.160.0) to match what the reference `model-editor` was written against.

The templates load these from `/template-assets/` (mapped to
`fused_render/templates/vendor/`), never from a CDN — same offline
self-containment rule as `scripts/vendor-sci/`.
