// import_part.mjs — pure GLB -> parts transform, the client-side port of
// project_ops.py:_import_part (specs/projects.md §5). No IO: takes decoded
// bytes in, returns each leaf part's re-encoded GLB bytes. Same shape
// discipline as sculpt_bake.mjs/placements.mjs — plain data in/out, no
// DOM/node builtins, so this file works unmodified from Node (this harness)
// or a browser importmap.
//
// importPart(glbBytes, dropName, existingPartNames) -> { parts: [{name, glbBytes}] }
//
// Steps (mirrors _import_part line-for-line, see specs/projects.md §5):
//  1. magic-byte + decoded-size validation, name sanitization/collision suffix
//  2. pre-joined multi-material mesh split (one node per primitive, named
//     after its material) — the bpy merged-export case
//  3. whole-group normalization matching compose.fit_to_height: uniform
//     scale to combined-bbox height 1.5, floor 0, center x/y — done here as
//     one global similarity transform composed onto every leaf's world
//     matrix, which is equivalent to Blender's per-top-level-object version
//     (same resulting world positions) but doesn't require mutating a scene
//     graph first.
//  4. node-tree flatten: each leaf's world transform is baked into its own
//     root node (no parent chain) — dropping non-mesh container nodes
//  5. per-leaf part naming + a bbox-center pivot root node, matching
//     compose.part's Empty (root at bbox center, child re-based under it)
//  6. one GLB emitted per leaf via a fresh Document (attributes, material
//     factors, and the core PBR texture maps — base color, metallic-
//     roughness, normal, occlusion, emissive — copied by value; only KHR
//     material extensions are still dropped, see NOTES.md)
//
// COORDINATE SPACES: compose.fit_to_height/compose.part operate on Blender's
// Z-up scene; this file operates directly on the Y-up glTF bytes. The axis
// mapping used below (glTF Y is Blender's "up"/floor axis; glTF X/Z are
// Blender's X/Y "horizontal" axes) is the standard Blender<->glTF exporter
// convention, so the same normalization reads as "fit height on the up axis,
// center the two horizontal axes" in either space.

import { Document, WebIO, VertexLayout } from '/template-assets/gltf-transform.bundle.mjs';

const MAX_IMPORT = 25 * 2 ** 20; // decoded bytes cap (project_ops.py _MAX_IMPORT)
const HEIGHT = 1.5;
const FLOOR = 0.0;

function sanitize(name) {
  return (name || '').replace(/[^A-Za-z0-9_-]/g, '');
}

function uniqueName(base, taken) {
  let name = base, n = 2;
  while (taken.has(name)) { name = `${base}-${n}`; n++; }
  taken.add(name);
  return name;
}

// ---- mat4 helpers (column-major 16-array, glTF/OpenGL convention) ----
function mat4Mul(a, b) {
  const out = new Array(16).fill(0);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[k * 4 + r] * b[c * 4 + k];
      out[c * 4 + r] = sum;
    }
  return out;
}
function mat4Translate(t) {
  return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, t[0], t[1], t[2], 1];
}
function mat4Scale(s) {
  return [s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1];
}
function transformPoint(m, p) {
  return [
    m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
    m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
    m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
  ];
}

function corners(min, max) {
  const out = [];
  for (const x of [min[0], max[0]])
    for (const y of [min[1], max[1]])
      for (const z of [min[2], max[2]])
        out.push([x, y, z]);
  return out;
}

// world-space bbox of a single leaf (world matrix already includes any
// normalization applied so far), from its primitives' local POSITION extents
function leafWorldBbox(primitives, worldMatrix) {
  let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const prim of primitives) {
    const pos = prim.getAttribute('POSITION');
    if (!pos) continue;
    const lmin = pos.getMin([]), lmax = pos.getMax([]);
    for (const c of corners(lmin, lmax)) {
      const w = transformPoint(worldMatrix, c);
      for (let i = 0; i < 3; i++) {
        if (w[i] < min[i]) min[i] = w[i];
        if (w[i] > max[i]) max[i] = w[i];
      }
    }
  }
  return { min, max };
}

function copyAccessor(destDoc, acc, buffer) {
  return destDoc.createAccessor(acc.getName())
    .setType(acc.getType())
    .setNormalized(acc.getNormalized())
    .setArray(acc.getArray().slice())
    .setBuffer(buffer);
}

// Copy one Texture (embedded image bytes + mime) into destDoc, deduped by
// source Texture so a map shared across slots/materials is embedded once.
function copyTexture(destDoc, tex, texCache) {
  if (!tex) return null;
  if (texCache.has(tex)) return texCache.get(tex);
  const img = tex.getImage();
  if (!img) return null;
  const t = destDoc.createTexture(tex.getName())
    .setImage(img.slice())
    .setMimeType(tex.getMimeType());
  texCache.set(tex, t);
  return t;
}

// Copy the TextureInfo sidecar (UV set + sampler wrap/filter) from a source
// slot onto the freshly-attached dest slot. Filters can be unset (null) — pass
// them through as-is so we don't invent a default the source didn't declare.
function copyTextureInfo(srcInfo, destInfo) {
  if (!srcInfo || !destInfo) return;
  destInfo.setTexCoord(srcInfo.getTexCoord());
  destInfo.setWrapS(srcInfo.getWrapS());
  destInfo.setWrapT(srcInfo.getWrapT());
  destInfo.setMagFilter(srcInfo.getMagFilter());
  destInfo.setMinFilter(srcInfo.getMinFilter());
}

function copyMaterial(destDoc, mat, matCache, texCache) {
  if (!mat) return null;
  if (matCache.has(mat)) return matCache.get(mat);
  // Copy the core metallic-roughness PBR surface: scalar/factor fields plus
  // every standard texture map (base color, metallic-roughness, normal,
  // occlusion, emissive) with their image bytes and TextureInfo. Without the
  // maps a textured model — whose baseColorFactor is white and whose color
  // lives entirely in the base-color texture — renders fully white after
  // import. KHR material extensions (specular-glossiness, transmission, …)
  // are still not ported; only core PBR is.
  const m = destDoc.createMaterial(mat.getName())
    .setBaseColorFactor(mat.getBaseColorFactor())
    .setMetallicFactor(mat.getMetallicFactor())
    .setRoughnessFactor(mat.getRoughnessFactor())
    .setEmissiveFactor(mat.getEmissiveFactor())
    .setAlphaMode(mat.getAlphaMode())
    .setAlphaCutoff(mat.getAlphaCutoff())
    .setDoubleSided(mat.getDoubleSided());

  const bc = copyTexture(destDoc, mat.getBaseColorTexture(), texCache);
  if (bc) { m.setBaseColorTexture(bc); copyTextureInfo(mat.getBaseColorTextureInfo(), m.getBaseColorTextureInfo()); }
  const mr = copyTexture(destDoc, mat.getMetallicRoughnessTexture(), texCache);
  if (mr) { m.setMetallicRoughnessTexture(mr); copyTextureInfo(mat.getMetallicRoughnessTextureInfo(), m.getMetallicRoughnessTextureInfo()); }
  const nt = copyTexture(destDoc, mat.getNormalTexture(), texCache);
  if (nt) { m.setNormalTexture(nt).setNormalScale(mat.getNormalScale()); copyTextureInfo(mat.getNormalTextureInfo(), m.getNormalTextureInfo()); }
  const oc = copyTexture(destDoc, mat.getOcclusionTexture(), texCache);
  if (oc) { m.setOcclusionTexture(oc).setOcclusionStrength(mat.getOcclusionStrength()); copyTextureInfo(mat.getOcclusionTextureInfo(), m.getOcclusionTextureInfo()); }
  const em = copyTexture(destDoc, mat.getEmissiveTexture(), texCache);
  if (em) { m.setEmissiveTexture(em); copyTextureInfo(mat.getEmissiveTextureInfo(), m.getEmissiveTextureInfo()); }

  matCache.set(mat, m);
  return m;
}

function copyPrimitive(destDoc, prim, matCache, texCache, buffer) {
  const dp = destDoc.createPrimitive().setMode(prim.getMode());
  for (const semantic of prim.listSemantics())
    dp.setAttribute(semantic, copyAccessor(destDoc, prim.getAttribute(semantic), buffer));
  const idx = prim.getIndices();
  if (idx) dp.setIndices(copyAccessor(destDoc, idx, buffer));
  const mat = copyMaterial(destDoc, prim.getMaterial(), matCache, texCache);
  if (mat) dp.setMaterial(mat);
  return dp;
}

function collectMeshNodes(nodes, acc) {
  for (const n of nodes) {
    if (n.getMesh()) acc.push(n);
    collectMeshNodes(n.listChildren(), acc);
  }
}

export async function importPart(glbBytes, dropName, existingPartNames = []) {
  if (glbBytes.length > MAX_IMPORT)
    throw new Error(`file too large (${Math.floor(glbBytes.length / 2 ** 20)} MB > ` +
      `${MAX_IMPORT / 2 ** 20} MB)`);
  const magic = String.fromCharCode(glbBytes[0], glbBytes[1], glbBytes[2], glbBytes[3]);
  if (magic !== 'glTF') throw new Error('not a binary glTF (.glb) file');

  const pnameSeed = sanitize(dropName);
  if (!pnameSeed) throw new Error(`can't derive a part name from ${JSON.stringify(dropName)}`);

  // separate (non-interleaved) vertex buffers: sculpt_bake.mjs patches part
  // GLBs in place and rejects interleaved attributes, so imported parts must
  // be written the way bpy freeze writes them or they can never be sculpted
  const io = new WebIO().setVertexLayout(VertexLayout.SEPARATE);
  const srcDoc = await io.readBinary(glbBytes);
  const scene = srcDoc.getRoot().listScenes()[0];
  const topNodes = scene ? scene.listChildren()
    : srcDoc.getRoot().listNodes().filter(n => !n.getParentNode());

  const meshNodes = [];
  collectMeshNodes(topNodes, meshNodes);
  if (meshNodes.length === 0) throw new Error('no meshes in the dropped file');

  // pre-joined export: one mesh, >=2 DISTINCT materials in use -> split into
  // one leaf per material (primitives sharing a material reference stay one
  // leaf/part — mirrors Blender's material_slots, which collapse primitives
  // that share a material into one slot; project_ops.py:256 splits on
  // `len(material_slots) > 1`, not raw primitive count). Each leaf named
  // after its material. (A single glTF *primitive* can only ever reference
  // one material, so the Python reference's other split branch — a
  // genuinely single-primitive multi-material mesh — has no reachable
  // analog here; see NOTES.md.)
  let leaves; // [{ primitives: Primitive[], worldMatrix, nameHint }]
  const distinctMaterials = meshNodes.length === 1
    ? new Set(meshNodes[0].getMesh().listPrimitives().map(p => p.getMaterial()))
    : null;
  if (meshNodes.length === 1 && distinctMaterials.size > 1) {
    const src = meshNodes[0];
    const wm = src.getWorldMatrix();
    const groups = new Map(); // material -> primitives[]
    for (const prim of src.getMesh().listPrimitives()) {
      const mat = prim.getMaterial();
      if (!groups.has(mat)) groups.set(mat, []);
      groups.get(mat).push(prim);
    }
    leaves = [...groups.entries()].map(([mat, primitives]) =>
      ({ primitives, worldMatrix: wm, nameHint: mat ? sanitize(mat.getName()) : '' }));
  } else {
    leaves = meshNodes.map(n => ({
      primitives: n.getMesh().listPrimitives(),
      worldMatrix: n.getWorldMatrix(),
      nameHint: sanitize(n.getName()),
    }));
  }

  // group normalization (compose.fit_to_height): uniform scale to combined
  // bbox height 1.5, floor 0, center the two horizontal axes. In glTF Y-up,
  // "height" is the Y extent.
  const bbox = leaves.reduce((acc, l) => {
    const b = leafWorldBbox(l.primitives, l.worldMatrix);
    return {
      min: acc.min.map((v, i) => Math.min(v, b.min[i])),
      max: acc.max.map((v, i) => Math.max(v, b.max[i])),
    };
  }, { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] });
  const yExtent = bbox.max[1] - bbox.min[1];
  const s = yExtent > 1e-9 ? HEIGHT / yExtent : 1.0;
  const pivot = [(bbox.min[0] + bbox.max[0]) / 2, bbox.min[1], (bbox.min[2] + bbox.max[2]) / 2];
  const target = [0, FLOOR, 0];
  // M(v) = target + (v - pivot) * s = Translate(target - s*pivot) * Scale(s)
  const t = [target[0] - s * pivot[0], target[1] - s * pivot[1], target[2] - s * pivot[2]];
  const M = mat4Mul(mat4Translate(t), mat4Scale(s));

  const taken = new Set(existingPartNames);
  const single = leaves.length === 1;
  const parts = [];

  for (const leaf of leaves) {
    const finalWorld = mat4Mul(M, leaf.worldMatrix);
    const partName = uniqueName(single ? pnameSeed : (leaf.nameHint || pnameSeed), taken);

    const leafBbox = leafWorldBbox(leaf.primitives, finalWorld);
    const pivotWorld = [
      (leafBbox.min[0] + leafBbox.max[0]) / 2,
      (leafBbox.min[1] + leafBbox.max[1]) / 2,
      (leafBbox.min[2] + leafBbox.max[2]) / 2,
    ];
    const childMatrix = mat4Mul(mat4Translate([-pivotWorld[0], -pivotWorld[1], -pivotWorld[2]]), finalWorld);

    const destDoc = new Document();
    const buffer = destDoc.createBuffer();
    const matCache = new Map();
    const texCache = new Map();
    const mesh = destDoc.createMesh(`${partName}_geo`);
    for (const prim of leaf.primitives) mesh.addPrimitive(copyPrimitive(destDoc, prim, matCache, texCache, buffer));

    const geoNode = destDoc.createNode(`${partName}_geo`).setMesh(mesh).setMatrix(childMatrix);
    const rootNode = destDoc.createNode(partName).addChild(geoNode)
      .setMatrix(mat4Translate(pivotWorld)).setExtras({ part_root: true });
    const destScene = destDoc.createScene().addChild(rootNode);
    destDoc.getRoot().setDefaultScene(destScene);

    const bytes = await io.writeBinary(destDoc);
    parts.push({ name: partName, glbBytes: bytes });
  }

  return { parts };
}
