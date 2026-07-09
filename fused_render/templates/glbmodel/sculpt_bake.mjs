// Client-side sculpt/paint bake (specs/viewer.md §6, §6b): dependency-free
// GLB byte surgery, a line-for-line port of sculpt_bake.py's bake() (same
// guards, same float64 math -> float32 packs — parity-proven by
// prototype_js_bake/parity.mjs). viewer.html runs this on the part GLB it
// loaded for the session and ships the patched bytes to sculpt_bake.py,
// which only validates and writes them to disk.
// API: bake(glbBytes: Uint8Array,
//           edits: {nodes: {name: {indices, deltas, colors, base_white}}})
//      -> {bytes: Uint8Array, patched: {name: touchedCount}}

const MAGIC = 0x46546c67;
const JSON_CHUNK = 0x4e4f534a;
const BIN_CHUNK = 0x004e4942;
const INDEX_BYTES = { 5121: 1, 5123: 2, 5125: 4 };

function readGlb(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (dv.getUint32(0, true) !== MAGIC) throw new Error('not a GLB file');
  let off = 12, gltf = null, blob = null;
  while (off < bytes.byteLength) {
    const clen = dv.getUint32(off, true);
    const ctype = dv.getUint32(off + 4, true);
    off += 8;
    if (ctype === JSON_CHUNK)
      gltf = JSON.parse(new TextDecoder().decode(bytes.subarray(off, off + clen)));
    else if (ctype === BIN_CHUNK)
      blob = bytes.slice(off, off + clen); // own copy, mutable + growable via realloc
    off += clen;
  }
  if (!gltf || !blob) throw new Error('missing JSON or BIN chunk');
  return [gltf, blob];
}

function writeGlb(gltf, blob) {
  let j = new TextEncoder().encode(JSON.stringify(gltf));
  const jPad = (4 - (j.length % 4)) % 4;
  const bPad = (4 - (blob.length % 4)) % 4;
  const total = 12 + 8 + j.length + jPad + 8 + blob.length + bPad;
  const out = new Uint8Array(total);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, MAGIC, true);
  dv.setUint32(4, 2, true);
  dv.setUint32(8, total, true);
  dv.setUint32(12, j.length + jPad, true);
  dv.setUint32(16, JSON_CHUNK, true);
  out.set(j, 20);
  out.fill(0x20, 20 + j.length, 20 + j.length + jPad); // JSON padded with spaces
  let o = 20 + j.length + jPad;
  dv.setUint32(o, blob.length + bPad, true);
  dv.setUint32(o + 4, BIN_CHUNK, true);
  out.set(blob, o + 8); // BIN padded with the fill's default zeros
  return out;
}

function accessorOffset(gltf, idx) {
  const acc = gltf.accessors[idx];
  const bv = gltf.bufferViews[acc.bufferView];
  if (bv.byteStride != null && bv.byteStride !== 12 && acc.type === 'VEC3')
    throw new Error('interleaved vertex buffers are unsupported');
  return [acc, (bv.byteOffset || 0) + (acc.byteOffset || 0)];
}

function triangles(gltf, blob, prim, vcount) {
  if (prim.indices == null) return Array.from({ length: vcount }, (_, i) => i);
  const [acc, off] = accessorOffset(gltf, prim.indices);
  const dv = new DataView(blob.buffer, blob.byteOffset);
  const size = INDEX_BYTES[acc.componentType];
  const get = size === 1 ? i => dv.getUint8(off + i)
    : size === 2 ? i => dv.getUint16(off + i * 2, true)
      : i => dv.getUint32(off + i * 4, true);
  const out = new Array(acc.count);
  for (let i = 0; i < acc.count; i++) out[i] = get(i);
  return out;
}

// area-weighted smooth normals, float64 accumulation like the python
function smoothNormals(pos, tris) {
  const n = new Float64Array(pos.length);
  for (let t = 0; t < tris.length; t += 3) {
    const a = tris[t], b = tris[t + 1], c = tris[t + 2];
    const ax = pos[a * 3], ay = pos[a * 3 + 1], az = pos[a * 3 + 2];
    const ux = pos[b * 3] - ax, uy = pos[b * 3 + 1] - ay, uz = pos[b * 3 + 2] - az;
    const vx = pos[c * 3] - ax, vy = pos[c * 3 + 1] - ay, vz = pos[c * 3 + 2] - az;
    const fx = uy * vz - uz * vy, fy = uz * vx - ux * vz, fz = ux * vy - uy * vx;
    for (const i of [a, b, c]) {
      n[i * 3] += fx; n[i * 3 + 1] += fy; n[i * 3 + 2] += fz;
    }
  }
  for (let i = 0; i < n.length; i += 3) {
    const ln = Math.sqrt(n[i] ** 2 + n[i + 1] ** 2 + n[i + 2] ** 2) || 1.0;
    n[i] /= ln; n[i + 1] /= ln; n[i + 2] /= ln;
  }
  return n;
}

function bakeColors(gltf, blob, prim, count, colors) {
  if (colors.length !== count)
    throw new Error(`colors length ${colors.length} != vertex count ${count}`);
  const attrs = prim.attributes;
  if ('COLOR_0' in attrs) {
    const acc = gltf.accessors[attrs.COLOR_0];
    if (acc.componentType !== 5126)
      throw new Error('non-float COLOR_0 accessors are unsupported');
    const bv = gltf.bufferViews[acc.bufferView];
    const ncomp = { VEC3: 3, VEC4: 4 }[acc.type];
    const stride = bv.byteStride || ncomp * 4;
    const off = (bv.byteOffset || 0) + (acc.byteOffset || 0);
    const dv = new DataView(blob.buffer, blob.byteOffset);
    for (let i = 0; i < count; i++) {
      dv.setFloat32(off + i * stride, colors[i][0], true);
      dv.setFloat32(off + i * stride + 4, colors[i][1], true);
      dv.setFloat32(off + i * stride + 8, colors[i][2], true);
    }
  } else {
    const pad = (4 - (blob.length % 4)) % 4;
    const off = blob.length + pad;
    const grown = new Uint8Array(off + count * 12);
    grown.set(blob, 0);
    const f32 = new Float32Array(grown.buffer, off, count * 3);
    for (let i = 0; i < count; i++) {
      f32[i * 3] = colors[i][0]; f32[i * 3 + 1] = colors[i][1]; f32[i * 3 + 2] = colors[i][2];
    }
    gltf.bufferViews.push({ buffer: 0, byteOffset: off, byteLength: count * 12 });
    gltf.accessors.push({
      bufferView: gltf.bufferViews.length - 1,
      componentType: 5126, count, type: 'VEC3',
    });
    attrs.COLOR_0 = gltf.accessors.length - 1;
    gltf.buffers[0].byteLength = grown.length;
    blob = grown;
  }
  return blob;
}

export function bake(glbBytes, edits) {
  let [gltf, blob] = readGlb(glbBytes);
  const byName = Object.fromEntries((gltf.nodes || []).map(n => [n.name, n]));
  const patched = {};
  for (const [name, ed] of Object.entries(edits.nodes)) {
    const node = byName[name];
    if (!node || node.mesh == null)
      throw new Error(`no mesh node '${name}' (nodes: ${Object.keys(byName).sort()})`);
    const prims = gltf.meshes[node.mesh].primitives;
    if (prims.length !== 1) throw new Error(`${name}: multi-primitive meshes are unsupported`);
    const prim = prims[0];
    const [pacc, poff] = accessorOffset(gltf, prim.attributes.POSITION);
    const count = pacc.count;
    let touched = 0;
    if (ed.indices && ed.indices.length) {
      const dv = new DataView(blob.buffer, blob.byteOffset);
      const pos = new Float64Array(count * 3); // float64 working copy like python's list
      for (let i = 0; i < count * 3; i++) pos[i] = dv.getFloat32(poff + i * 4, true);
      for (let k = 0; k < ed.indices.length; k++) {
        const i = ed.indices[k];
        pos[i * 3] += ed.deltas[k][0];
        pos[i * 3 + 1] += ed.deltas[k][1];
        pos[i * 3 + 2] += ed.deltas[k][2];
      }
      for (let i = 0; i < count * 3; i++) dv.setFloat32(poff + i * 4, pos[i], true);
      // min/max over the float32-rounded values, matching python (it reads
      // back nothing — but packs from float64; min/max computed on float64).
      const mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
      for (let i = 0; i < count * 3; i++) {
        const a = i % 3;
        if (pos[i] < mn[a]) mn[a] = pos[i];
        if (pos[i] > mx[a]) mx[a] = pos[i];
      }
      pacc.min = mn; pacc.max = mx;
      if ('NORMAL' in prim.attributes) {
        const [, noff] = accessorOffset(gltf, prim.attributes.NORMAL);
        const normals = smoothNormals(pos, triangles(gltf, blob, prim, count));
        for (let i = 0; i < count * 3; i++) dv.setFloat32(noff + i * 4, normals[i], true);
      }
      touched += ed.indices.length;
    }
    if (ed.colors) {
      blob = bakeColors(gltf, blob, prim, count, ed.colors);
      if (ed.base_white && prim.material != null) {
        const mat = gltf.materials[prim.material];
        (mat.pbrMetallicRoughness ??= {}).baseColorFactor = [1.0, 1.0, 1.0, 1.0];
      }
      touched += count;
    }
    patched[name] = touched;
  }
  return { bytes: writeGlb(gltf, blob), patched };
}
