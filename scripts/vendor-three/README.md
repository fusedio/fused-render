# vendor-three

Build workspace that bundles the 3D libraries used by the `glb` (read-only
viewer) and `glbmodel` (interactive editor) templates into self-contained ESM
bundles committed under `fused_render/templates/vendor/`:

- `three.bundle.mjs` — Three.js core + `OrbitControls` + `TransformControls` +
  `GLTFLoader`, re-exported by `three-entry.mjs`.
- `gltf-transform.bundle.mjs` — `@gltf-transform/core` (`Document`, `WebIO`,
  `VertexLayout`), re-exported by `gltf-transform-entry.mjs`, used by the
  editor's client-side GLB import.

## Rebuild

```sh
./build.sh
```

Needs `bun` on PATH. Installs the pinned deps and runs esbuild. Only the two
built `*.bundle.mjs` files are committed — `node_modules/` and `bun.lockb` are
git-ignored (see `.gitignore`). The versions are pinned in `package.json`
(three 0.160.0, @gltf-transform/core 4.4.1) to match what the reference
`model-editor` was written against.

The templates load these from `/template-assets/` (mapped to
`fused_render/templates/vendor/`), never from a CDN — same offline
self-containment rule as `scripts/vendor-sci/`.
