// Entry for three.bundle.mjs — the Three.js surface both glb templates use,
// re-exported from one self-contained ESM bundle so /render pages load it from
// /template-assets/ with no CDN (same offline rule as geotiff/pdfjs/zarr).
//
// The reference viewer.html imported these four from an esm.sh import map:
//   import * as THREE from 'three';
//   import { OrbitControls }   from 'three/addons/controls/OrbitControls.js';
//   import { TransformControls } from 'three/addons/controls/TransformControls.js';
//   import { GLTFLoader }      from 'three/addons/loaders/GLTFLoader.js';
// Vendored, that becomes a single named import from this bundle:
//   import { THREE, OrbitControls, TransformControls, GLTFLoader } from '/template-assets/three.bundle.mjs';
export * as THREE from 'three';
export { OrbitControls } from 'three/addons/controls/OrbitControls.js';
export { TransformControls } from 'three/addons/controls/TransformControls.js';
export { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
