// Whether the collapsed Claude rail on a canvas page shows the chat under the
// pointer as a live floating card (RailPeek.tsx) — the `canvas_rail_peek_enabled`
// preference (shell/prefs.py), default OFF.
//
// The shape of `./feature-flag.ts` (the Canvases switch itself): one shared
// prefs GET, a generation guard so a publish beats a slower in-flight read,
// and `null` meaning "not asked yet", which renders as off — the flag's own
// default, so a rail never grows a card the reader turned off because a GET
// was still out.
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
      if (generation === departed) set(p.canvas_rail_peek?.enabled === true);
    })
    .catch(() => {
      // Not an answer: stay off (the default) and let the next mount ask again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer — the prefs payload the Settings switch's PUT
 *  returned — so a canvas page in this tab follows the switch at once. */
export function publishCanvasRailPeekEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useCanvasRailPeek(): boolean {
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

/** Test-only: forget the cached answer so a suite starts from "not asked". */
export function resetCanvasRailPeekFlagForTests() {
  reading = null;
  generation += 1;
  enabled = null;
  for (const listener of listeners) listener(false);
}
