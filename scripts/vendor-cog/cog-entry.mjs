// Entry for cog.bundle.mjs — everything the map viewer needs from deck.gl,
// including the browser Cloud-Optimized GeoTIFF reader, in ONE bundle.
//
// deck.gl and the COG layer must come from the same bundle: two copies of
// luma.gl in a page fail with "This version of luma.gl has already been
// initialized", so the map viewer cannot load deck.gl separately.
//
// Vendored as a single named import from this bundle:
//   import { MapboxOverlay, COGLayer, ... } from '/template-assets/cog.bundle.mjs';
export { MapboxOverlay } from '@deck.gl/mapbox';
export { BitmapLayer, GeoJsonLayer, ScatterplotLayer } from 'deck.gl';
export { COGLayer, MultiCOGLayer } from '@developmentseed/deck.gl-geotiff';
// Shader modules, so contrast and colour are GPU uniforms rather than a
// round trip to a tile server: restyling re-renders without re-fetching.
export * as gpu from '@developmentseed/deck.gl-raster/gpu-modules';
// The 107-ramp colormap sprite (generated from rio-tiler's own colormap list)
// inlines as a data URL — 16KB, so it costs one fewer request than a fetch.
export { default as colormapsUrl } from '@developmentseed/deck.gl-raster/gpu-modules/colormaps.png';
export { DecoderPool, GeoTIFF } from '@developmentseed/geotiff';
export { parseWkt } from '@developmentseed/proj';
