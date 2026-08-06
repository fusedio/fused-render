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

export function formatMtime(epochSeconds: number | null | undefined): string {
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
