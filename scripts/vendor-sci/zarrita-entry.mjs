// Bundled into fused_render/templates/vendor/zarrita.bundle.mjs. zarr_template.html
// does `import * as zarr from "…"` and uses zarr.withMaybeConsolidatedMetadata /
// open / root / get. Re-export the whole namespace. The zstd/blosc codecs pull in
// numcodecs, whose WASM is inlined as base64 — the bundle stays self-contained.
export * from "zarrita";
