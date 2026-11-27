// The `canvases_enabled` preference (D427, shell/prefs.py) for code that may
// not import apps/canvases (scripts/check-boundaries.mjs) — today the explorer
// home search's "S3" group (apps/explorer/RemoteHitsGroup.tsx).
//
// The same module-level cache + subscribe shape as apps/canvases/feature-flag.ts
// and apps/explorer/lib/ranked-search-pref.ts: one read per page load (through
// the single-flight `getPrefsShared`, so it shares the GET those modules fire),
// a generation guard so a read that departed before a publish cannot overwrite
// it, and `publishCanvasesFlag` for the Preferences toggle. That toggle calls
// apps/canvases/feature-flag.ts's `publishCanvasesEnabled`, which forwards here,
// so a reader of this module hears it at once.
//
// Default OFF, like the canvases flag itself: null/unread renders as off.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(p.canvases.enabled);
    })
    .catch(() => {
      // A failed read is not an answer: stay off and let the next mount retry.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the Preferences toggle's PUT reply). */
export function publishCanvasesFlag(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. Off until the first read lands. */
export function useCanvasesFlag(): boolean {
  const [current, setCurrent] = useState(enabled === true);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled === true);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
