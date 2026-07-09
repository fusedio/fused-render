// Client-side placement math (specs/viewer.md §5): dependency-free quaternion
// helpers and the three.js-delta -> Blender-space override conversion, plus
// the placements.json merge rules used by Save. Pure functions of plain
// numbers/arrays — no THREE, no DOM — so this module is importable from
// plain node/bun (prototype_js_bake-style parity harnesses) as well as the
// browser (viewer.html dynamic-imports it via fused.rawUrl).
// API:
//   qMultiply(a, b) -> [w,x,y,z]
//   qNormalize(q) -> [w,x,y,z]
//   eulerDegToQuat(rx, ry, rz) -> [w,x,y,z]   (Blender's own Euler XYZ semantics, degrees)
//   deltaToOverride(entry, delta) -> entry    (mutates+returns the overrides.json entry)
//   mergePlacement(p, override) -> p          (mutates+returns the placements.json entry)
//   blenderToThreeDelta(entry) -> {dp, dq, ms} (inverse of deltaToOverride's axis map,
//                                                for composing a placements.json/overrides.json
//                                                entry onto a three.js part node)

// --- quaternion helpers on plain [w,x,y,z] arrays (Blender-space rq field,
// specs/workflow.md §3b) — no library, this repo's only quaternion math
// outside THREE's own (used for the capture side, see accumulateDelta) ---
export function qMultiply(a, b) {   // a ⊗ b, both [w,x,y,z]
  const [aw, ax, ay, az] = a, [bw, bx, by, bz] = b;
  return [
    aw * bw - ax * bx - ay * by - az * bz,
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
  ];
}

export function qNormalize(q) {
  const n = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
  return [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
}

// Blender Euler XYZ semantics (degrees) -> quaternion [w,x,y,z] — for
// converting a stored legacy rx/ry/rz placement (interpreted by
// model_ops.scene.apply_overrides via bpy's own Euler('XYZ').to_quaternion())
// when composing a new rq onto it. Composition is q = qz ⊗ qy ⊗ qx — NOT
// three.js's Euler('XYZ') (q = qx ⊗ qy ⊗ qz); "XYZ" is an ambiguous label
// across libraries, and these placements.json entries are Blender-space, so
// Blender's convention is the correct one here. Verified numerically against
// bpy's Euler.to_quaternion() by placement_parity/parity.mjs (see NOTES.md).
export function eulerDegToQuat(rx, ry, rz) {
  const d2r = Math.PI / 180;
  const hx = rx * d2r / 2, hy = ry * d2r / 2, hz = rz * d2r / 2;
  const qx = [Math.cos(hx), Math.sin(hx), 0, 0];
  const qy = [Math.cos(hy), 0, Math.sin(hy), 0];
  const qz = [Math.cos(hz), 0, 0, Math.sin(hz)];
  return qMultiply(qMultiply(qz, qy), qx);
}

// Convert a three.js-space transform delta (position xyz + quaternion xyzw,
// all plain numbers; scale ratios per axis in three.js space) into the
// Blender-space overrides.json entry, composing onto any existing `entry`.
// Axis map: bx=dx, by=-dz, bz=dy; quat (x,y,z,w) -> [w,x,-z,y].
// Returns false (leaving entry untouched) if the delta is near-identity.
export function deltaToOverride(entry, delta) {
  const { dp, dq, ms } = delta; // dp:{x,y,z}, dq:{x,y,z,w}, ms:[sx,sy,sz] (three.js axes)
  const dqIdentity = Math.abs(Math.abs(dq.w) - 1) < 1e-7;
  const dpLenSq = dp.x * dp.x + dp.y * dp.y + dp.z * dp.z;
  if (dpLenSq < 1e-10 && dqIdentity
      && ms.every(v => Math.abs(v - 1) < 1e-6)) return false;
  entry.x = +((entry.x || 0) + dp.x).toFixed(4);
  entry.y = +((entry.y || 0) - dp.z).toFixed(4);
  entry.z = +((entry.z || 0) + dp.y).toFixed(4);
  const dqB = [dq.w, dq.x, -dq.z, dq.y];
  entry.rq = qNormalize(qMultiply(dqB, entry.rq ?? [1, 0, 0, 0]))
    .map(v => +v.toFixed(5));
  entry.sx = +((entry.sx ?? 1) * ms[0]).toFixed(4);
  entry.sy = +((entry.sy ?? 1) * ms[2]).toFixed(4);
  entry.sz = +((entry.sz ?? 1) * ms[1]).toFixed(4);
  for (const k of ['x', 'y', 'z']) if (!entry[k]) delete entry[k];
  if (entry.rq && Math.abs(Math.abs(entry.rq[0]) - 1) < 1e-6) delete entry.rq;
  for (const k of ['sx', 'sy', 'sz']) if (entry[k] === 1) delete entry[k];
  return entry;
}

// Inverse of deltaToOverride's axis map (specs/viewer.md model-loading
// section): given a Blender-space placements.json/overrides.json entry
// (`x/y/z`, `rq: [w,x,y,z]`, `sx/sy/sz` — any subset, absent keys default to
// identity), return the equivalent three.js-space delta
// `{dp:{x,y,z}, dq:{x,y,z,w}, ms:[sx,sy,sz]}` to compose onto a part node's
// live transform (position add, quaternion premultiply, scale multiply —
// same composition order `apply_overrides` uses Blender-side). Round-trips
// exactly with deltaToOverride's forward map (placement_parity/parity.mjs).
export function blenderToThreeDelta(entry = {}) {
  const rq = entry.rq ?? [1, 0, 0, 0];
  return {
    dp: { x: entry.x || 0, y: entry.z || 0, z: -(entry.y || 0) },
    dq: { w: rq[0], x: rq[1], y: rq[3], z: -rq[2] },
    ms: [entry.sx ?? 1, entry.sz ?? 1, entry.sy ?? 1],
  };
}

// Merge one overrides.json delta entry `d` into a placements.json entry `p`
// (mutated in place, also returned): locations add, rq premultiplies
// (converting legacy rx/ry/rz once via eulerDegToQuat then deleting them),
// scale multiplies, material keys replace.
export function mergePlacement(p, d) {
  for (const k of ['x', 'y', 'z'])                              // deltas add
    if (d[k]) p[k] = +((p[k] || 0) + d[k]).toFixed(4);
  if (d.rq) {   // compose by quaternion premultiply; convert legacy euler once
    const legacyQ = (p.rx || p.ry || p.rz)
      ? eulerDegToQuat(p.rx || 0, p.ry || 0, p.rz || 0) : null;
    p.rq = qNormalize(qMultiply(d.rq, p.rq ?? legacyQ ?? [1, 0, 0, 0]))
      .map(v => +v.toFixed(5));
    delete p.rx; delete p.ry; delete p.rz;
  }
  for (const k of ['sx', 'sy', 'sz'])                           // multipliers
    if (d[k] != null) p[k] = +((p[k] ?? 1) * d[k]).toFixed(4);
  for (const k of ['color', 'roughness', 'metallic'])           // absolutes
    if (d[k] != null) p[k] = d[k];
  return p;
}
