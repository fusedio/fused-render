import type { AppInfo } from "@platform/lib/api";
import type { AppsSort } from "../layout";

const label = (a: AppInfo) => a.title || a.name;

/** Home apps first (already in the widget's sort), then every catalog app not
    already listed, deduped by path. A "name" sort re-sorts the merged list. */
export function mergeApps(home: AppInfo[], all: AppInfo[], sort: AppsSort): AppInfo[] {
  const seen = new Set(home.map((a) => a.path));
  const out = [...home];
  for (const a of all) {
    if (seen.has(a.path)) continue;
    seen.add(a.path);
    out.push(a);
  }
  return sort === "name" ? out.sort((x, y) => label(x).localeCompare(label(y))) : out;
}
