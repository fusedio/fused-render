// Pure formatting helpers. No DOM, no fetch. (The vanilla module also carried
// escapeHtml — dropped: JSX escapes text content itself.)
export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes;
  let u = -1;
  do {
    v /= 1024;
    u++;
  } while (v >= 1024 && u < units.length - 1);
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[u]}`;
}

// Parameter counts, the unit models are compared in — "7.2B", "465M". Distinct
// from formatSize: these are counts, not bytes, so the steps are decimal (a
// "7B model" is 7e9 parameters, never 7 * 2^30) and the unit is never implied
// by the number alone.
export function formatParams(count: number | null | undefined): string {
  if (!count || count < 0) return "";
  if (count >= 1e9) return `${Number((count / 1e9).toFixed(1))}B`;
  if (count >= 1e6) return `${Math.round(count / 1e6)}M`;
  if (count >= 1e3) return `${Math.round(count / 1e3)}K`;
  return `${count}`;
}

// Listing-grade stamp: locale date + hours:minutes. Seconds are noise in a
// column of file dates — and carrying them made MODIFIED the widest column in
// the table, which is backwards for the least important one. The full
// precision is still one hover away (the cells carry formatMtimeFull as their
// title) and one panel away (Preview's stat card uses it outright).
export function formatMtime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

// Full precision, seconds included — for tooltips and the stat panel.
export function formatMtimeFull(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString();
}

// "3d ago" style stamp; null when no time is known.
export function timeAgo(epochSeconds: number | null | undefined): string | null {
  if (!epochSeconds) return null;
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

export function basename(fsPath: string): string {
  const parts = fsPath.split("/").filter((s) => s.length > 0);
  return parts.length ? parts[parts.length - 1] : "/";
}

export function dirname(fsPath: string): string {
  const idx = fsPath.replace(/\/+$/, "").lastIndexOf("/");
  if (idx <= 0) return idx === 0 ? "/" : "";
  return fsPath.slice(0, idx);
}
