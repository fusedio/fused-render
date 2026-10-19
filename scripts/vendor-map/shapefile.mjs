import proj4 from 'proj4';
import shp from 'shpjs';

const SHAPE_TYPES = new Set([0, 1, 3, 5, 8, 11, 13, 15, 18, 21, 23, 25, 28]);
const LANGUAGE_DRIVERS = { 0: 'iso-8859-1', 3: 'windows-1252', 87: 'windows-1252' };

function dbfEncoding(dbf, cpg) {
  const label = cpg ? new TextDecoder().decode(cpg).trim().toLowerCase() : '';
  if (!label) return LANGUAGE_DRIVERS[new DataView(dbf).getUint8(29)] ?? null;
  const iso = label.match(/^8859(\d+)$/);
  const name = iso ? `iso-8859-${iso[1]}` : /^\d+$/.test(label) ? `windows-${label}` : label;
  try { return new TextDecoder(name).encoding; } catch (_) { return null; }
}

function dbfIsPlain(dbf) {
  const view = new DataView(dbf);
  const records = view.getUint32(4, true), headerLen = view.getUint16(8, true), recLen = view.getUint16(10, true);
  let fields = 0;
  while (32 * (fields + 1) < view.byteLength && view.getUint8(32 * (fields + 1)) !== 0x0d) fields++;
  if (headerLen !== 32 * (fields + 1) + 1 || headerLen + records * recLen > view.byteLength) return false;
  for (let i = 0; i < records; i++) if (view.getUint8(headerLen + i * recLen) === 0x2a) return false;
  return true;
}

function plainValue(value) {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return null;
    const pad = n => String(n).padStart(2, '0');
    return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
  }
  return typeof value === 'number' && !Number.isFinite(value) ? null : value;
}

export async function shapefileToGeoJSON({ shp: geometry, dbf, prj, cpg }) {
  if (!SHAPE_TYPES.has(new DataView(geometry).getInt32(32, true))) return null;
  const encoding = dbf && dbfEncoding(dbf, cpg);
  if (dbf && (!encoding || !dbfIsPlain(dbf))) return null;
  const wkt = prj ? new TextDecoder().decode(prj).trim() : '';
  if (wkt) {
    try { proj4(wkt); } catch (_) { return null; }
  }
  const collection = await shp({ shp: geometry, dbf, cpg: encoding || undefined, prj: wkt || undefined });
  for (const feature of collection.features) {
    for (const key in feature.properties) feature.properties[key] = plainValue(feature.properties[key]);
  }
  return collection;
}
