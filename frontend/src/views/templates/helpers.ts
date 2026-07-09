import type { KeyKind, RegistryResult } from "../../lib/api";

// Download via a synthetic <a download> click — the export endpoint streams the
// zip as an attachment, so no fetch/blob dance is needed.
export function triggerDownload(url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export const sourceLabel = (registry: RegistryResult, id: string): string =>
  registry.sources.find((s) => s.id === id)?.label ?? id;

// -- key builder (create mode) ----------------------------------------------

const SEGMENT = /^[A-Za-z0-9_-]+$/;

// Compute the key string + a client-side validity check for a chosen shape and
// the literal the user typed. The server validates authoritatively (§2.3); this
// is just live feedback.
export function buildKey(kind: KeyKind, raw: string): { key: string; error: string | null } {
  const literal = raw.trim().replace(/^\.+/, "").replace(/\/+$/, "");
  const segs = literal.split(".").filter((s) => s.length > 0);
  const segsOk = segs.length > 0 && segs.every((s) => SEGMENT.test(s));
  if (kind === "simple") {
    if (segs.length !== 1 || !segsOk) return { key: "." + literal, error: "Enter one extension, e.g. csv" };
    return { key: "." + segs[0], error: null };
  }
  if (kind === "compound") {
    if (segs.length < 2 || !segsOk)
      return { key: "." + literal, error: "Enter at least two segments, e.g. geo.parquet" };
    return { key: "." + segs.join("."), error: null };
  }
  if (kind === "wildcard") {
    if (segs.length < 1 || !segsOk)
      return { key: ".*." + literal, error: "Enter the literal part after the wildcard, e.g. json" };
    return { key: ".*." + segs.join("."), error: null };
  }
  // directory
  if (segs.length !== 1 || !segsOk)
    return { key: "." + literal + "/", error: "Enter one extension, e.g. zarr" };
  return { key: "." + segs[0] + "/", error: null };
}

export const KEY_KINDS: { kind: KeyKind; label: string; hint: string }[] = [
  { kind: "simple", label: "Simple", hint: ".ext" },
  { kind: "compound", label: "Compound", hint: ".a.b" },
  { kind: "wildcard", label: "Wildcard", hint: ".*.json" },
  { kind: "directory", label: "Directory", hint: ".ext/" },
];

// -- Section A: bindings table shared type -----------------------------------

export type BindFilter = "all" | "modified";
