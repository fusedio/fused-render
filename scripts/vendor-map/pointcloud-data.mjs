// Bounded statistics; styling never allocates an RGBA array per cloud point.
export function sampledBounds(data, scheme, options = {}) {
  const range = options.colorRange;
  const intensity = scheme === 'intensity';
  const values = [];
  const count = Math.min(data.pointCount, 16384);
  for (let j = 0; j < count; j++) {
    const i = Math.floor(j * data.pointCount / count);
    const v = intensity ? data.intensities?.[i] : data.positions[i * 3 + 2];
    if (Number.isFinite(v)) values.push(v);
  }
  values.sort((a, b) => a - b);
  const full = intensity
    ? {min: values[0] ?? 0, max: values.at(-1) ?? 1}
    : {min: data.bounds?.minZ ?? values[0] ?? 0, max: data.bounds?.maxZ ?? values.at(-1) ?? 1};
  if (range?.mode === 'absolute') return {
    min: range.absoluteMin ?? full.min, max: range.absoluteMax ?? full.max,
  };
  if (options.usePercentile === false && !range) return full;
  if (!values.length) return full;
  const at = p => values[Math.floor((values.length - 1) * Math.max(0, Math.min(100, p)) / 100)];
  return {min: at(range?.percentileLow ?? 2), max: at(range?.percentileHigh ?? 98)};
}

export function colorLookup(ramp) {
  const rgba = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const x = i / 255 * (ramp.length - 1), lo = Math.floor(x), hi = Math.ceil(x);
    for (let c = 0; c < 3; c++) rgba[i * 4 + c] = Math.round(ramp[lo][c] * (hi - x || 1) + (hi === lo ? 0 : ramp[hi][c] * (x - lo)));
    rgba[i * 4 + 3] = 255;
  }
  return rgba;
}

export function classLookup(hidden, getColor) {
  const rgba = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    rgba.set(getColor(i), i * 4);
    rgba[i * 4 + 3] = hidden.has(i) ? 0 : 255;
  }
  return rgba;
}
