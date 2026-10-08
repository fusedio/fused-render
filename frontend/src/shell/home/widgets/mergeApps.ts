import type { AppInfo } from "@platform/lib/api";

/** Home apps first (already in the widget's sort), then every catalog app not
    already listed, in catalog order, deduped by path. Never re-sorted: catalog
    pages arrive lazily and a re-sort would make visible tiles jump. */
export function mergeApps(home: AppInfo[], all: AppInfo[]): AppInfo[] {
  const seen = new Set(home.map((a) => a.path));
  const out = [...home];
  for (const a of all) {
    if (seen.has(a.path)) continue;
    seen.add(a.path);
    out.push(a);
  }
  return out;
}
