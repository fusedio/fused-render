import { asyncBufferFromUrl, parquetMetadataAsync, parquetRead, parquetReadObjects, parquetSchema } from "hyparquet";
import { compressors } from "hyparquet-compressors";

const MAX_VALUES = 200000;
const VALUE_BATCH = 3;
const FEATURES_PER_CHUNK = 20000;
const HEAVY_BYTES_PER_ROW = 8;
const MAX_GROUP_RAW_BYTES = 128 << 20;
const MAX_GROUP_ROWS = 2000000;
const BOX = ["xmin", "ymin", "xmax", "ymax"];
const files = new Map();
const reads = new Map();
let nextRead = 0;

const PRIMITIVE = new Set(["BOOLEAN", "INT32", "INT64", "INT96", "FLOAT", "DOUBLE"]);
const TEXT_LOGICAL = new Set(["STRING", "ENUM", "JSON", "UUID", "DECIMAL"]);
const TEXT_CONVERTED = new Set(["UTF8", "ENUM", "JSON", "DECIMAL"]);
const LONLAT_CODES = new Set(["EPSG:4326", "OGC:CRS84", "OGC:CRS83", "EPSG:4269", "EPSG:4979"]);

function crsIsLonLat(crs) {
  if (crs == null) return true;
  if (typeof crs === "string") return LONLAT_CODES.has(crs.toUpperCase().replace(/\s+/g, ""));
  const id = crs.id || (Array.isArray(crs.ids) && crs.ids[0]);
  if (id && id.authority && id.code !== undefined) return LONLAT_CODES.has(`${String(id.authority).toUpperCase()}:${id.code}`);
  return crs.type === "GeographicCRS" && /WGS\s*84|CRS\s*84/i.test(String(crs.name || ""));
}

const boxArea = b => Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
const isLonLatBox = b => !!b && b.every(Number.isFinite) && b[0] >= -180.5 && b[2] <= 180.5 && b[1] >= -90.5 && b[3] <= 90.5
  && b[0] <= b[2] && b[1] <= b[3];

function textual(element) {
  const logical = element.logical_type && element.logical_type.type;
  return TEXT_LOGICAL.has(logical) || TEXT_CONVERTED.has(element.converted_type);
}

function displayable(child) {
  return child.children.length
    ? child.children.some(displayable)
    : PRIMITIVE.has(child.element.type) || textual(child.element);
}

function describe(metadata, budget) {
  const top = parquetSchema(metadata).children;
  let geo = null;
  try { geo = JSON.parse((metadata.key_value_metadata || []).find(kv => kv.key === "geo").value); } catch (_) {}
  const isGeometryType = child => /^(GEOMETRY|GEOGRAPHY)$/.test(child.element.logical_type && child.element.logical_type.type);
  const geometry = top.find(child => !child.children.length && child.element.name === (geo && geo.primary_column))
    || top.find(child => !child.children.length && isGeometryType(child));
  if (!geometry) return { problem: "no GeoParquet geometry column" };
  const geometryName = geometry.element.name;
  const column = (geo && geo.columns && geo.columns[geometryName]) || {};
  const logical = geometry.element.logical_type || {};
  const encoding = column.encoding || (isGeometryType(geometry) ? "WKB" : null);
  if (encoding !== "WKB") return { problem: `geometry encoded as ${encoding || "unknown"}, not WKB` };
  if (!crsIsLonLat("crs" in column ? column.crs : logical.crs)) return { problem: "geometry is in a projected CRS" };

  let paths = null;
  let bboxColumn = null;
  const covering = column.covering && column.covering.bbox;
  if (covering && BOX.every(k => Array.isArray(covering[k]))) {
    paths = BOX.map(k => covering[k].join("."));
    bboxColumn = covering.xmin[0];
  } else {
    const struct = top.find(child => (child.element.name === "bbox" || child.element.name === `${geometryName}_bbox`)
      && BOX.every(k => child.children.some(c => c.element.name === k)));
    if (struct) {
      paths = BOX.map(k => `${struct.element.name}.${k}`);
      bboxColumn = struct.element.name;
    }
  }

  const attributes = top.filter(child => child !== geometry && child.element.name !== bboxColumn && displayable(child));
  const columns = attributes
    .filter(child => !child.children.length)
    .map(child => child.element.name);

  const columnBytes = new Map([[geometryName, 0], ...columns.map(name => [name, 0])]);
  let start = 0;
  const rowGroups = metadata.row_groups.map((group, index) => {
    const rows = Number(group.num_rows);
    const chunks = new Map();
    const bytes = new Map();
    const raw = new Map();
    for (const { meta_data: chunk } of group.columns) {
      chunks.set(chunk.path_in_schema.join("."), chunk);
      const name = chunk.path_in_schema[0];
      if (columnBytes.has(name)) {
        const size = Number(chunk.total_compressed_size);
        columnBytes.set(name, columnBytes.get(name) + size);
        bytes.set(name, (bytes.get(name) || 0) + size);
        raw.set(name, (raw.get(name) || 0) + Number(chunk.total_uncompressed_size));
      }
    }
    let bbox = paths && paths.map((path, k) => {
      const stats = chunks.get(path) && chunks.get(path).statistics;
      const value = stats && (k < 2 ? stats.min_value ?? stats.min : stats.max_value ?? stats.max);
      return value == null ? NaN : Number(value);
    });
    if (!bbox || !bbox.every(Number.isFinite)) {
      const native = chunks.get(geometryName) && chunks.get(geometryName).geospatial_statistics;
      bbox = native && native.bbox && BOX.map(k => native.bbox[k]);
      if (bbox && !bbox.every(Number.isFinite)) bbox = null;
    }
    const out = { index, rows, start, bbox: bbox || null, bytes, raw };
    start += rows;
    return out;
  });

  const rowCount = Math.max(1, Number(metadata.num_rows));
  const fields = columns.filter(name => columnBytes.get(name) / rowCount <= HEAVY_BYTES_PER_ROW);
  const drawn = [geometryName, ...fields];
  const drawnSize = sizes => drawn.reduce((sum, name) => sum + (sizes.get(name) || 0), 0);
  for (const group of rowGroups) {
    group.bytes = drawnSize(group.bytes);
    group.raw = drawnSize(group.raw);
  }
  const groups = rowGroups.filter(g => g.rows > 0);
  if (!groups.length || !groups.every(g => isLonLatBox(g.bbox))) return { problem: "its row groups carry no longitude/latitude bounding boxes" };
  if (groups.some(g => g.raw > MAX_GROUP_RAW_BYTES || g.rows > MAX_GROUP_ROWS)) return { problem: "a row group is too large to draw at once" };
  const boxes = groups.map(g => g.bbox);
  const extent = boxes.reduce((e, b) => [Math.min(e[0], b[0]), Math.min(e[1], b[1]), Math.max(e[2], b[2]), Math.max(e[3], b[3])],
                              [Infinity, Infinity, -Infinity, -Infinity]);
  const areas = boxes.map(boxArea).sort((a, b) => a - b);
  if (groups.reduce((sum, g) => sum + g.bytes, 0) > budget
      && (groups.some(g => g.bytes > budget) || areas[Math.floor(areas.length / 2)] > boxArea(extent) / 4)) {
    return { problem: "its row groups are too broad to narrow a view down" };
  }
  return {
    geometryName, fields, rowGroups, extent,
    extraFields: attributes.map(child => child.element.name).filter(name => !fields.includes(name)),
    geometryTypes: Array.isArray(column.geometry_types) ? column.geometry_types : [],
    rows: Number(metadata.num_rows),
  };
}

function plainValue(value) {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "bigint") return Number.isSafeInteger(Number(value)) ? Number(value) : value.toString();
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? undefined : value.toISOString();
  if (ArrayBuffer.isView(value)) return undefined;
  try { return JSON.stringify(value, (_, v) => typeof v === "bigint" ? v.toString() : v); } catch (_) { return undefined; }
}

function properties(row, names) {
  const out = {};
  for (const name of names) {
    const value = plainValue(row[name]);
    if (value !== undefined) out[name] = value;
  }
  return out;
}

async function open({ url, budget, key }) {
  let whole = false;
  const fetchNoting = async (...args) => {
    const response = await fetch(...args);
    if (response.status === 200 && new Headers(args[1] && args[1].headers).has("range")) whole = true;
    return response;
  };
  const file = await asyncBufferFromUrl({ url, fetch: fetchNoting });
  const metadata = await parquetMetadataAsync(file);
  const summary = describe(metadata, budget);
  if (summary.problem) return summary;
  files.set(key, { url, metadata, summary, byteLength: file.byteLength, whole: whole ? file : null });
  return { key, ...summary };
}

function handle(key) {
  const entry = files.get(key);
  if (!entry) throw new Error("This Parquet file is no longer open.");
  return entry;
}

const readGroup = ({ metadata, file }, group, columns) =>
  parquetReadObjects({ file, metadata, columns, compressors, rowStart: group.start, rowEnd: group.start + group.rows });

async function reading(key, tag, run) {
  const entry = handle(key);
  const controller = new AbortController();
  const id = `${key}:${tag}`;
  reads.set(id, controller);
  try {
    const file = entry.whole || await asyncBufferFromUrl({ url: entry.url, byteLength: entry.byteLength,
                                                          requestInit: { signal: controller.signal } });
    return await run({ ...entry, file });
  } catch (error) {
    if (controller.signal.aborted) return null;
    throw error;
  } finally {
    reads.delete(id);
  }
}

function read({ key, index }) {
  return reading(key, index, async entry => {
    const { summary } = entry, group = summary.rowGroups[index];
    const rows = await readGroup(entry, group, [summary.geometryName, ...summary.fields]);
    const encoder = new TextEncoder();
    const parts = [encoder.encode('{"type":"FeatureCollection","features":[')];
    const types = new Set();
    let batch = [];
    let count = 0;
    const flush = () => {
      if (batch.length) parts.push(encoder.encode((parts.length > 1 ? "," : "") + batch.join(",")));
      batch = [];
    };
    rows.forEach((row, k) => {
      const geometry = row[summary.geometryName];
      if (!geometry || !geometry.type) return;
      types.add(geometry.type);
      batch.push(JSON.stringify({ type: "Feature", id: group.start + k, geometry, properties: properties(row, summary.fields) }));
      count += 1;
      if (batch.length === FEATURES_PER_CHUNK) flush();
    });
    flush();
    parts.push(encoder.encode("]}"));
    const blob = new Blob(parts, { type: "application/json" });
    return { blob, size: blob.size, count, types: [...types] };
  });
}

function values({ key, indexes, field }) {
  return reading(key, `values-${++nextRead}`, async ({ metadata, summary, file }) => {
    if (!summary.fields.includes(field)) return [];
    const out = [];
    const collect = ({ columnData }) => {
      for (const raw of columnData) {
        if (out.length >= MAX_VALUES) return;
        const value = plainValue(raw);
        if (value !== undefined) out.push(value);
      }
    };
    for (let k = 0; k < indexes.length && out.length < MAX_VALUES; k += VALUE_BATCH) {
      await Promise.all(indexes.slice(k, k + VALUE_BATCH).map(index => {
        const group = summary.rowGroups[index];
        return parquetRead({ file, metadata, columns: [field], compressors, onChunk: collect,
                             rowStart: group.start, rowEnd: group.start + group.rows });
      }));
    }
    return out;
  });
}

function row({ key, row: index }) {
  return reading(key, `row-${++nextRead}`, async ({ metadata, summary, file }) => {
    const columns = summary.extraFields;
    if (!columns.length) return {};
    const [values] = await parquetReadObjects({ file, metadata, columns, compressors, useOffsetIndex: true,
                                                rowStart: index, rowEnd: index + 1 });
    return properties(values || {}, columns);
  });
}

function cancel({ key, index }) {
  const controller = reads.get(`${key}:${index}`);
  if (controller) controller.abort();
  return !!controller;
}

function close({ key }) {
  for (const [id, controller] of reads) if (id.startsWith(`${key}:`)) controller.abort();
  return files.delete(key);
}

const OPS = { open, read, values, row, cancel, close };

self.onmessage = async ({ data }) => {
  const { id, op, ...args } = data;
  try {
    const result = await OPS[op](args);
    self.postMessage({ id, ok: true, result });
  } catch (error) {
    self.postMessage({ id, ok: false, error: error && error.message || String(error) });
  }
};
