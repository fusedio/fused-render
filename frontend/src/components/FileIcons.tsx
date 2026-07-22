// Per-filetype icons for the file explorer (Listing rows + search results) and
// the sidebar's Fused folder link. Small monochrome inline SVGs in the same
// house style as FinderIcon/SplitIcons: 16x16, viewBox 0 0 24 24, fill none,
// stroke currentColor, round caps/joins — hand-written Lucide-geometry paths,
// no npm dependency. Per-category tint comes from CSS (.file-icon--<variant>
// in shell.css); the SVG itself is colourless and inherits via currentColor.
//
// iconForEntry(name, isDir) maps a filename's extension to one of ~12 category
// icons; extensions are grouped from fused_render/templates/registry.json so
// the icon and the file's preview template agree on what kind of file it is.
import type { ReactNode } from "react";

const svgProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

// Variant = the CSS tint bucket + the shape drawn. One glyph per bucket keeps
// the set legible (no per-language fragmentation).
type Variant =
  | "folder"
  | "code"
  | "data"
  | "json"
  | "html"
  | "image"
  | "doc"
  | "media"
  | "geo"
  | "archive"
  | "db"
  | "file";

function Glyph({ variant, children }: { variant: Variant; children: ReactNode }) {
  return (
    <svg className={"file-icon file-icon--" + variant} {...svgProps}>
      {children}
    </svg>
  );
}

// Folder (dirs) — the classic tabbed folder.
export function FolderIcon() {
  return (
    <Glyph variant="folder">
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
    </Glyph>
  );
}

// Open book — the sidebar's "Learn" entry (bundled learn.zip mount).
export function LearnIcon() {
  return (
    <Glyph variant="doc">
      <path d="M12 7v13" />
      <path d="M3 6a2 2 0 0 1 2-2h4a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H3Z" />
      <path d="M21 6a2 2 0 0 0-2-2h-4a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H21Z" />
    </Glyph>
  );
}

// Code / config / shell / stylesheet — file with a </> glyph inside.
function CodeIcon() {
  return (
    <Glyph variant="code">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path d="m10 12.5-2 2.5 2 2.5" />
      <path d="m14 12.5 2 2.5-2 2.5" />
    </Glyph>
  );
}

// Tabular data (parquet, csv/tsv, xlsx, netcdf, zarr) — a table/grid so
// columnar formats like parquet read as data at a glance.
function DataIcon() {
  return (
    <Glyph variant="data">
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <path d="M3 9h18" />
      <path d="M3 15h18" />
      <path d="M12 3v18" />
    </Glyph>
  );
}

// Structured text (json / jsonl / ndjson) — braces.
function JsonIcon() {
  return (
    <Glyph variant="json">
      <path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1" />
      <path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1" />
    </Glyph>
  );
}

// HTML / htm — a rounded app tile with a play triangle: html files are
// launchable apps here, so the icon reads "run me", not "web document".
function HtmlIcon() {
  return (
    <Glyph variant="html">
      <rect width="18" height="18" x="3" y="3" rx="4" />
      <path d="M10 8.5v7l6-3.5Z" />
    </Glyph>
  );
}

// Raster/vector images — picture frame with a sun + peak.
function ImageIcon() {
  return (
    <Glyph variant="image">
      <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
    </Glyph>
  );
}

// Documents / prose (pdf, md, txt, log) — file with text lines.
function DocIcon() {
  return (
    <Glyph variant="doc">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path d="M16 13H8" />
      <path d="M16 17H8" />
      <path d="M10 9H8" />
    </Glyph>
  );
}

// Audio / video — film strip.
function MediaIcon() {
  return (
    <Glyph variant="media">
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <path d="M7 3v18" />
      <path d="M3 7.5h4" />
      <path d="M3 12h18" />
      <path d="M3 16.5h4" />
      <path d="M17 3v18" />
      <path d="M17 7.5h4" />
      <path d="M17 16.5h4" />
    </Glyph>
  );
}

// Geospatial / vector (geojson, shp, kml, gpkg, fgb, pmtiles, geotiff, las) —
// a map pin.
function GeoIcon() {
  return (
    <Glyph variant="geo">
      <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
      <circle cx="12" cy="10" r="3" />
    </Glyph>
  );
}

// Archives (zip, tar, gz, whl, jar, egg, …) — a lidded box.
function ArchiveIcon() {
  return (
    <Glyph variant="archive">
      <rect width="20" height="5" x="2" y="3" rx="1" />
      <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
      <path d="M10 12h4" />
    </Glyph>
  );
}

// Databases (sqlite/db) — cylinder.
function DbIcon() {
  return (
    <Glyph variant="db">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M3 5v14a9 3 0 0 0 18 0V5" />
      <path d="M3 12a9 3 0 0 0 18 0" />
    </Glyph>
  );
}

// Fallback — plain file outline (dimmest).
function FileIcon() {
  return (
    <Glyph variant="file">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    </Glyph>
  );
}

// Extension (sans leading dot, lower-cased) -> variant. Grouped from
// registry.json's template assignments so an icon matches how the file opens.
const EXT_VARIANT: Record<string, Variant> = {
  // code / config / shell / style
  py: "code",
  js: "code",
  ts: "code",
  tsx: "code",
  jsx: "code",
  cjs: "code",
  mjs: "code",
  cts: "code",
  mts: "code",
  sh: "code",
  zsh: "code",
  fish: "code",
  ps1: "code",
  csh: "code",
  "zsh-theme": "code",
  vim: "code",
  yaml: "code",
  yml: "code",
  toml: "code",
  ini: "code",
  cfg: "code",
  conf: "code",
  tf: "code",
  hcl: "code",
  css: "code",
  plist: "code",
  // tabular / gridded data
  parquet: "data",
  csv: "data",
  tsv: "data",
  xlsx: "data",
  nc: "data",
  nc4: "data",
  cdf: "data",
  zgroup: "data",
  zattrs: "data",
  zmetadata: "data",
  // structured
  json: "json",
  jsonl: "json",
  ndjson: "json",
  // web
  html: "html",
  htm: "html",
  // images
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  svg: "image",
  // documents / prose
  pdf: "doc",
  md: "doc",
  markdown: "doc",
  txt: "doc",
  log: "doc",
  // audio / video
  mp4: "media",
  mov: "media",
  m4v: "media",
  webm: "media",
  mp3: "media",
  wav: "media",
  m4a: "media",
  ogg: "media",
  flac: "media",
  // geospatial / vector
  geojson: "geo",
  shp: "geo",
  kml: "geo",
  kmz: "geo",
  gpx: "geo",
  gpkg: "geo",
  fgb: "geo",
  pmtiles: "geo",
  tif: "geo",
  tiff: "geo",
  las: "geo",
  laz: "geo",
  // archives
  zip: "archive",
  jar: "archive",
  whl: "archive",
  egg: "archive",
  tar: "archive",
  tgz: "archive",
  tbz2: "archive",
  txz: "archive",
  gz: "archive",
  bz2: "archive",
  xz: "archive",
  // databases
  sqlite: "db",
  sqlite3: "db",
  db: "db",
};

// Extension of a filename, sans dot, lower-cased. A leading-dot name with no
// other dot (".gitignore") has no extension -> fallback file icon.
function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return "";
  return name.slice(dot + 1).toLowerCase();
}

const RENDER: Record<Variant, () => JSX.Element> = {
  folder: FolderIcon,
  code: CodeIcon,
  data: DataIcon,
  json: JsonIcon,
  html: HtmlIcon,
  image: ImageIcon,
  doc: DocIcon,
  media: MediaIcon,
  geo: GeoIcon,
  archive: ArchiveIcon,
  db: DbIcon,
  file: FileIcon,
};

// Pick the icon for a listing entry: directories always get the folder icon;
// files map by extension, falling back to a plain file outline.
export function iconForEntry(name: string, isDir: boolean): JSX.Element {
  const variant: Variant = isDir ? "folder" : (EXT_VARIANT[extOf(name)] ?? "file");
  return RENDER[variant]();
}

// html/htm files open as launchable apps — callers use this to badge rows and
// switch "Open" wording to "Open App".
export function isAppEntry(name: string, isDir: boolean): boolean {
  return !isDir && EXT_VARIANT[extOf(name)] === "html";
}
