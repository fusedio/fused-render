import { useEffect, useState } from "react";
import { getConfig } from "@platform/lib/api";

// Same "~" contraction the top-bar Breadcrumb, the panel crumb strip, and the
// framed-panel crumb strip each compute inline (Breadcrumb.tsx,
// listing/path-crumbs.tsx, Panel.tsx) — strictly below home only, since home
// itself shows its full path, not a lone "~". This copy exists for the
// search-hit table's "Path in ~/…" header; the three existing call sites are
// untouched.
export function contractHome(fsPath: string, home: string | undefined): string {
  if (home !== undefined && fsPath.startsWith(home + "/")) {
    return "~" + fsPath.slice(home.length);
  }
  return fsPath;
}

// The one `/api/config` lookup every merged-field host (Listing.tsx's folder
// box, FileSearchField.tsx's file box) needs purely for `contractHome`
// above — a config fetch neither view has any other reason to make. A
// module-level cache rather than a fetch per host: the two are SIBLINGS
// across a navigation (a folder's Listing unmounts the moment the file it
// contains mounts FileSearchField, and vice versa), so whichever host
// resolved `home` first hands every later mount the answer on its very
// first render, with no second round trip and no repeat of the
// undefined-then-rewritten flash. `undefined` means unresolved, exactly as
// before a host's own fetch used to land — every crumb strip already
// renders the full path in that state, so a mount that finds the cache
// still empty behaves exactly as it did fetching solo.
let cachedHome: string | undefined;
let inFlight: Promise<void> | null = null;
const listeners = new Set<(next: string | undefined) => void>();

function setCachedHome(next: string): void {
  if (cachedHome === next) return;
  cachedHome = next;
  for (const listen of listeners) listen(next);
}

function fetchHome(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = getConfig()
    .then((c) => setCachedHome(c.home.replace(/\\/g, "/")))
    .catch(() => {
      // Left unresolved — a host that never gets an answer keeps rendering
      // full paths, same as before this fetch had a shared cache. Cleared
      // so a later mount (or the same one, on a future render) gets to try
      // again rather than being stuck on one failed attempt forever.
      inFlight = null;
    });
  return inFlight;
}

export function useHome(): string | undefined {
  const [home, setHome] = useState<string | undefined>(cachedHome);
  useEffect(() => {
    if (cachedHome !== undefined) {
      setHome(cachedHome);
      return;
    }
    listeners.add(setHome);
    void fetchHome();
    return () => {
      listeners.delete(setHome);
    };
  }, []);
  return home;
}

// Test seam: the cache outlives any component, so a test that resolves it
// must be able to put it back for the next one.
export function resetHome(): void {
  cachedHome = undefined;
  inFlight = null;
  listeners.clear();
}
