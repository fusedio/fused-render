// Bundled into fused_render/templates/vendor/geotiff.bundle.mjs. esbuild emits an
// ESM module; geotiff_template.html does `import * as geotiff from "…"` and uses
// geotiff.fromArrayBuffer / geotiff.fromUrl. Re-export the whole namespace so any
// geotiff API the template reaches for is present.
export * from "geotiff";
