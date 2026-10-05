// Entry for map.bundle.mjs — the map viewer's whole rendering stack in ONE
// bundle. Everything draws in the browser straight from the file (HTTP range
// reads, GPU shaders, in-browser tiling); there is no tile server.
//
//   maplibre-gl           the map itself (one copy: the vector and zarr layers
//                         register protocols/custom layers on THIS instance)
//   maplibre-gl-raster    GeoTIFF/COG/VRT/STAC/MosaicJSON read over HTTP range
//                         requests and drawn on the GPU through deck.gl
//                         (headless LayerManager; bands/rescale/colormap/
//                         stretch/gamma/nodata are shader uniforms)
//   maplibre-gl-vector    GeoJSON natively; every other format (GeoParquet,
//                         Shapefile, GeoPackage, FlatGeobuf, CSV, KML...) via
//                         DuckDB-WASM, tiled in the browser with ST_AsMVT
//   @carbonplan/zarr-layer Zarr v2/v3 as a MapLibre custom layer (zarrita)
//   pmtiles               the pmtiles:// protocol
//   maplibre-gl-lidar     LAS/LAZ/COPC/EPT point clouds through deck.gl's
//                         PointCloudLayer; COPC and EPT stream by viewport
//                         (octree nodes by range read), LAS/LAZ load whole
//
// deck.gl comes only from here (via maplibre-gl-raster); two copies of luma.gl
// in one page fail with "This version of luma.gl has already been initialized".
import maplibregl from 'maplibre-gl';
export { maplibregl };
export { LayerManager as RasterManager, summarizeGeoTIFF, readPixelValues,
         sampleColormapStops, isKnownColormap, statsForBand,
         COLORMAP_NAMES, COLORMAP_DISPLAY_NAMES } from 'maplibre-gl-raster';
export { VectorControl } from 'maplibre-gl-vector';
export { ZarrLayer } from '@carbonplan/zarr-layer';
// The same zarrita the zarr layer reads with, for the page's own metadata pass
// (variables, dimension labels) — one copy, and its stores are interchangeable.
export * as zarr from 'zarrita';
export { LidarControl } from './pointcloud-gpu.mjs';
export { getClassificationName, getClassificationColor,
         COLORMAP_NAMES as LIDAR_COLORMAPS } from 'maplibre-gl-lidar';
export { Protocol as PMTilesProtocol, PMTiles, FetchSource } from 'pmtiles';
