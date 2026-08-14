// Entry for three.bundle.mjs — the Three.js surface the read-only glb viewer
// uses, re-exported from one self-contained ESM bundle so /render pages load it
// from /template-assets/ with no CDN (same offline rule as geotiff/pdfjs/zarr).
//
// Vendored as a single named import from this bundle:
//   import { THREE, OrbitControls, GLTFLoader } from '/template-assets/three.bundle.mjs';
export * as THREE from 'three';
export { OrbitControls } from 'three/addons/controls/OrbitControls.js';
export { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
